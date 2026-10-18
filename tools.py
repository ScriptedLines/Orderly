import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
from langchain_classic.retrievers import ContextualCompressionRetriever
from langchain_core.output_parsers import StrOutputParser
from langchain_core.tools import BaseTool, tool
from config import llm, logger, reranker, supabase_client, vector_store
from prompt import qa_prompt
from schemas import (
    CancelOrderInput,
    CancelOrderResponse,
    ChatResponse,
    CheckReturnEligibilityInput,
    CheckReturnEligibilityResponse,
    EscalateToHumanInput,
    EscalateToHumanResponse,
    ItemReturnEligibility,
    ListCustomerOrdersInput,
    ListCustomerOrdersResponse,
    OrderData,
    OrderDetailsResponse,
    OrderItemData,
    OrderLookupInput,
    OrderSummaryData,
    PolicySearchInput,
    ShipmentData,
    SourceCitation,
)


def _find_order_with_auth_check(
    order_id: Union[str, int], user_id: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Locate an order by order_number or UUID and verify it belongs to user_id

    in a single query to ensure security and prevent order enumeration.
    """
    clean_id = str(order_id).strip()
    digits_only = clean_id.lstrip("#").strip()

    is_num = digits_only.isdigit()
    col = "order_number" if is_num else "id"
    val = int(digits_only) if is_num else clean_id

    try:
        res = (
            supabase_client.table("orders")
            .select("*")
            .eq(col, val)
            .eq("customer_id", user_id)
            .execute()
        )
        if not res.data:
            return None, f"Order '{order_id}' was not found for your account. Please verify your order number."

        return res.data[0], None
    except Exception as e:
        logger.error(f"Error querying order '{order_id}' for user '{user_id}': {e}")
        return None, f"Error retrieving order '{order_id}'."


def _get_order_full_details_sync(order: Dict[str, Any]) -> OrderData:
    """Fetch all items, products, specs, and shipments for a given order, returning an OrderData Pydantic model."""
    order_pk = order["id"]

    items_res = (
        supabase_client.table("order_items")
        .select(
            "id, quantity, unit_price, product_id, "
            "products(id, name, category, price, description, specs, is_final_sale)"
        )
        .eq("order_id", order_pk)
        .execute()
    )
    items = items_res.data or []

    shipments_res = (
        supabase_client.table("shipments")
        .select("id, carrier, tracking_number, shipped_at, expected_delivery, last_status, updated_at")
        .eq("order_id", order_pk)
        .order("shipped_at", desc=True)
        .execute()
    )
    shipments = shipments_res.data or []

    formatted_items: List[OrderItemData] = []
    for it in items:
        p = it.get("products") if isinstance(it.get("products"), dict) else {}
        qty = it.get("quantity", 1)
        unit_price = float(it.get("unit_price") or 0.0)
        formatted_items.append(
            OrderItemData(
                product_id=it.get("product_id"),
                product_name=p.get("name", "Unknown Product"),
                category=p.get("category", ""),
                quantity=qty,
                unit_price=unit_price,
                subtotal=round(qty * unit_price, 2),
                is_final_sale=bool(p.get("is_final_sale", False)),
                description=p.get("description", ""),
                specs=p.get("specs") if isinstance(p.get("specs"), dict) else {},
            )
        )

    formatted_shipments: List[ShipmentData] = []
    for s in shipments:
        formatted_shipments.append(
            ShipmentData(
                carrier=s.get("carrier"),
                tracking_number=s.get("tracking_number"),
                shipped_at=s.get("shipped_at"),
                expected_delivery=str(s.get("expected_delivery")) if s.get("expected_delivery") else None,
                last_status=s.get("last_status"),
                updated_at=s.get("updated_at"),
            )
        )

    return OrderData(
        order_number=order["order_number"],
        order_id=order["id"],
        status=order["status"],
        placed_at=order["placed_at"],
        delivered_at=order.get("delivered_at"),
        total=float(order.get("total") or 0.0),
        currency=order.get("currency", "INR"),
        item_count=len(formatted_items),
        items=formatted_items,
        shipments=formatted_shipments,
    )


@tool(args_schema=PolicySearchInput)
async def search_order_policies(query: str) -> ChatResponse:
    """Search and extract store policies regarding orders, shipping, deliveries, returns,

    refunds, exchanges, cancellations, warranties, payments, or terms of service.
    """
    try:
        base_retriever = vector_store.as_retriever(
            search_type="similarity",
            search_kwargs={
                "k": 10,
                "filter": {"status": "active"},
            },
        )
        compression_retriever = ContextualCompressionRetriever(
            base_compressor=reranker,
            base_retriever=vector_store.as_retriever(
                search_type="similarity",
                search_kwargs={
                    "k": 10,
                    "filter": {"status": "active"},
                }
            ),
        )
        relevant_chunks = await compression_retriever.ainvoke(
            query,
            config={
                "run_name": "Order Policy Retrieval",
                "tags": ["rag", "order_policy", "retrieval", "rerank"],
            },
        )

        if not relevant_chunks:
            fallback_ans = "INSUFFICIENT_CONTEXT: No relevant order or store policy documents found to answer this query."
            return ChatResponse(answer=fallback_ans, sources=[])

        context = "\n\n".join(
            f"[{idx + 1}] Source: {doc.metadata.get('source', 'Unknown')}\n{doc.page_content}"
            for idx, doc in enumerate(relevant_chunks)
        )

        citations = [
            SourceCitation(
                source=doc.metadata.get("source", "Unknown"),
                chunk_excerpt=doc.page_content[:200] + "..." if len(doc.page_content) > 200 else doc.page_content,
            )
            for doc in relevant_chunks[:5]
        ]

        qa_chain = qa_prompt | llm | StrOutputParser()

        full_ans = await qa_chain.ainvoke(
            {
                "query": query,
                "context": context,
            },
            config={
                "run_name": "Order Policy QA Generation",
                "tags": ["rag", "order_policy", "tool_generation"],
            },
        )

        return ChatResponse(answer=full_ans, sources=citations)
    except Exception as e:
        logger.exception(f"Error executing order policy pipeline for query '{query}': {e}")
        raise


def mark_thread_escalated(
    thread_id: str,
    user_id: str,
    reason: str,
    summary: str = "",
) -> EscalateToHumanResponse:
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        res = (
            supabase_client.table("conversations")
            .select("all_messages")
            .eq("thread_id", thread_id)
            .eq("user_id", user_id)
            .execute()
        )
        messages = []
        if res.data:
            messages = res.data[0].get("all_messages") or []
            if not isinstance(messages, list):
                messages = []

        escalation_record = {
            "role": "system",
            "type": "escalation",
            "status": "escalated_to_human",
            "thread_completed": True,
            "reason": reason,
            "summary": summary,
            "timestamp": now_iso,
        }
        messages.append(escalation_record)

        supabase_client.table("conversations").update({
            "all_messages": messages,
            "updated_at": now_iso,
        }).eq("thread_id", thread_id).execute()

        logger.info(f"Thread '{thread_id}' for user '{user_id}' escalated to human: {reason}")
    except Exception as e:
        logger.error(f"Error marking thread '{thread_id}' as escalated in database: {e}")

    customer_message = (
        "Your request has been escalated to a human support agent. "
        "A member of our team will review the conversation and assist you shortly. "
        "This chat session has now ended."
    )

    return EscalateToHumanResponse(
        success=True,
        escalated=True,
        thread_completed=True,
        thread_id=thread_id,
        message=customer_message,
        reason=reason,
    )


def is_thread_completed(thread_id: str, user_id: Optional[str] = None) -> bool:
    """Helper to check if a conversation thread has been completed/escalated and is unusable for further bot queries."""
    try:
        query = supabase_client.table("conversations").select("all_messages").eq("thread_id", thread_id)
        if user_id:
            query = query.eq("user_id", user_id)
        res = query.execute()
        if not res.data:
            return False

        messages = res.data[0].get("all_messages") or []
        for msg in messages:
            if isinstance(msg, dict) and msg.get("thread_completed") is True:
                return True
        return False
    except Exception as e:
        logger.error(f"Error checking completion status for thread '{thread_id}': {e}")
        return False


def get_customer_support_tools(
    user_id: str,
    thread_id: str,
    order_id: Optional[str] = None,
) -> List[BaseTool]:
    """Factory creating customer support tools with user_id, thread_id, and order_id bound from the session.

    The LLM tool schemas do not expose user_id or order_id, preventing tampering or parameter hallucination.
    """

    @tool("get_order_details", args_schema=OrderLookupInput)
    async def get_order_details() -> OrderDetailsResponse:
        """Retrieve complete details for the customer's current order."""
        if not order_id:
            return OrderDetailsResponse(
                success=False,
                error="No order ID was provided for this conversation. Please specify or select an order."
            )
        order, err = _find_order_with_auth_check(order_id, user_id)
        if err or not order:
            return OrderDetailsResponse(success=False, error=err or "Order not found.")
        details = await asyncio.to_thread(_get_order_full_details_sync, order)
        return OrderDetailsResponse(success=True, data=details)

    @tool("list_customer_orders", args_schema=ListCustomerOrdersInput)
    async def list_customer_orders(limit: int = 5) -> ListCustomerOrdersResponse:
        """Retrieve recent orders placed by the customer."""
        def _fetch():
            return (
                supabase_client.table("orders")
                .select(
                    "id, order_number, status, placed_at, delivered_at, total, currency, "
                    "order_items(products(name))"
                )
                .eq("customer_id", user_id)
                .order("placed_at", desc=True)
                .limit(limit)
                .execute()
            )

        res = await asyncio.to_thread(_fetch)
        raw_orders = res.data or []
        if not raw_orders:
            return ListCustomerOrdersResponse(
                success=True,
                count=0,
                message="No orders were found for your account.",
                orders=[],
            )

        summaries: List[OrderSummaryData] = []
        for o in raw_orders:
            item_names: List[str] = []
            for it in o.get("order_items") or []:
                prod = it.get("products") or {}
                if prod.get("name"):
                    item_names.append(prod["name"])
            summaries.append(
                OrderSummaryData(
                    order_number=o["order_number"],
                    order_id=o["id"],
                    status=o["status"],
                    placed_at=o["placed_at"],
                    delivered_at=o.get("delivered_at"),
                    total=float(o.get("total") or 0.0),
                    currency=o.get("currency", "INR"),
                    items=item_names,
                )
            )

        return ListCustomerOrdersResponse(
            success=True,
            count=len(summaries),
            orders=summaries,
        )

    @tool("cancel_order", args_schema=CancelOrderInput)
    async def cancel_order(reason: Optional[str] = "") -> CancelOrderResponse:
        """Cancel the customer's current order."""
        if not order_id:
            return CancelOrderResponse(
                success=False,
                error="No order was selected for this session.",
                message="No order was selected for this session.",
            )
        order, err = _find_order_with_auth_check(order_id, user_id)
        if err or not order:
            return CancelOrderResponse(
                success=False,
                error=err or "Order not found.",
                message=err or "Order not found.",
            )

        status = order.get("status")
        order_num = order.get("order_number")
        if status == "cancelled":
            return CancelOrderResponse(
                success=False,
                error="Order is already cancelled.",
                message=f"Order #{order_num} is already cancelled.",
                order_number=order_num,
                current_status="cancelled",
            )
        if status not in ["placed", "processing"]:
            return CancelOrderResponse(
                success=False,
                error=f"Order cannot be cancelled in '{status}' status.",
                message=f"Order #{order_num} cannot be cancelled because it is already '{status}'. Only orders in 'placed' or 'processing' status can be cancelled.",
                order_number=order_num,
                current_status=status,
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        def _update():
            return (
                supabase_client.table("orders")
                .update({"status": "cancelled", "updated_at": now_iso})
                .eq("id", order["id"])
                .eq("customer_id", user_id)
                .execute()
            )

        up_res = await asyncio.to_thread(_update)
        if not up_res.data:
            return CancelOrderResponse(
                success=False,
                error="Failed to update order status.",
                message=f"Failed to cancel order #{order_num}.",
                order_number=order_num,
                current_status=status,
            )

        return CancelOrderResponse(
            success=True,
            message=f"Order #{order_num} has been successfully cancelled.",
            order_number=order_num,
            previous_status=status,
            current_status="cancelled",
            reason=reason or None,
        )

    @tool("check_order_return_eligibility", args_schema=CheckReturnEligibilityInput)
    async def check_order_return_eligibility() -> CheckReturnEligibilityResponse:
        """Check return eligibility for items in the customer's current order."""
        if not order_id:
            return CheckReturnEligibilityResponse(
                eligible=False,
                error="No order was selected for this session.",
                message="No order was selected for this session.",
            )
        order, err = _find_order_with_auth_check(order_id, user_id)
        if err or not order:
            return CheckReturnEligibilityResponse(
                eligible=False,
                error=err or "Order not found.",
                message=err or "Order not found.",
            )

        status = order.get("status")
        order_num = order.get("order_number")
        delivered_at = order.get("delivered_at")

        if status != "delivered":
            return CheckReturnEligibilityResponse(
                eligible=False,
                order_number=order_num,
                order_status=status,
                message=f"Order #{order_num} is currently '{status}'. Items can only be returned after the order is delivered.",
            )

        def _fetch_items():
            return (
                supabase_client.table("order_items")
                .select("quantity, unit_price, products(id, name, is_final_sale)")
                .eq("order_id", order["id"])
                .execute()
            )

        items_res = await asyncio.to_thread(_fetch_items)
        items = items_res.data or []

        days_since_delivery = None
        if delivered_at:
            try:
                del_dt = datetime.fromisoformat(delivered_at.replace("Z", "+00:00"))
                days_since_delivery = (datetime.now(timezone.utc) - del_dt).days
            except Exception:
                days_since_delivery = None

        past_return_window = (days_since_delivery is not None and days_since_delivery > 30)

        item_eligibilities: List[ItemReturnEligibility] = []
        any_returnable = False

        for it in items:
            prod = it.get("products") or {}
            p_name = prod.get("name", "Unknown Product")
            final_sale = bool(prod.get("is_final_sale", False))
            qty = it.get("quantity", 1)

            if past_return_window:
                ret = False
                note = f"Ineligible: {days_since_delivery} days since delivery exceeds 30-day return window."
            elif final_sale:
                ret = False
                note = "Ineligible: Marked as Final Sale item."
            else:
                ret = True
                note = "Eligible for return."
                any_returnable = True

            item_eligibilities.append(
                ItemReturnEligibility(
                    product_name=p_name,
                    quantity=qty,
                    is_final_sale=final_sale,
                    returnable=ret,
                    note=note,
                )
            )

        if past_return_window:
            overall_msg = (
                f"Order #{order_num} was delivered {days_since_delivery} days ago. "
                "This exceeds the 30-day return window."
            )
        elif any_returnable:
            overall_msg = f"Order #{order_num} is within the return window. Eligible items can be returned."
        else:
            overall_msg = f"No items in order #{order_num} are eligible for return."

        return CheckReturnEligibilityResponse(
            eligible=any_returnable,
            order_number=order_num,
            order_status=status,
            delivered_at=delivered_at,
            days_since_delivery=days_since_delivery,
            message=overall_msg,
            items=item_eligibilities,
        )

    @tool("escalate_to_human", args_schema=EscalateToHumanInput, return_direct=True)
    async def escalate_to_human(reason: str, summary: Optional[str] = "") -> EscalateToHumanResponse:
        """Escalate the conversation to a human support agent and mark the thread completed."""
        return await asyncio.to_thread(mark_thread_escalated, thread_id, user_id, reason, summary or "")

    return [
        get_order_details,
        list_customer_orders,
        cancel_order,
        check_order_return_eligibility,
        search_order_policies,
        escalate_to_human,
    ]


find_order_with_auth_check = _find_order_with_auth_check

