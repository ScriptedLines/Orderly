import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    messages_from_dict,
    messages_to_dict,
)

from langchain.agents import create_agent
from config import llm, logger, supabase_client
from dependencies import AuthenticatedUser, get_current_user
from prompt import AGENT_SYSTEM_PROMPT
from schemas import ChatRequest, CreateThreadRequest, CreateThreadResponse
from tools import (
    find_order_with_auth_check,
    get_customer_support_tools,
    is_thread_completed,
    mark_thread_escalated,
)

router = APIRouter(tags=["Chat"])



def create_customer_support_agent(
    user_id: str,
    thread_id: str,
    order_id: Optional[str] = None,
):
    """Create a LangChain agent graph that manages tool calls, state, and execution loops internally."""
    tools = get_customer_support_tools(
        user_id=user_id,
        thread_id=thread_id,
        order_id=order_id,
    )

    return create_agent(
        model=llm,
        tools=tools,
        system_prompt=AGENT_SYSTEM_PROMPT,
    )


async def _chat_stream_generator(
    user_id: str,
    thread_id: str,
    query: str,
    order_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Execute the customer support agent loop, stream tokens via SSE,

    enforce the 20-turn human escalation threshold, and persist message history to Supabase.
    """
    try:
        if is_thread_completed(thread_id, user_id):
            closed_msg = (
                "This conversation has already been escalated to a human support agent "
                "and is closed for automated responses."
            )
            yield f"data: {json.dumps({'type': 'error', 'detail': closed_msg})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'thread_id': thread_id, 'escalated': True})}\n\n"
            return

        def _fetch_conversation() -> List[Dict[str, Any]]:
            res = (
                supabase_client.table("conversations")
                .select("all_messages")
                .eq("thread_id", thread_id)
                .eq("user_id", user_id)
                .execute()
            )
            if not res.data:
                return []
            msgs = res.data[0].get("all_messages",[])
            return msgs 

        raw_messages: List[Dict[str, Any]] = await asyncio.to_thread(_fetch_conversation)
        clean_raw = [
            m for m in raw_messages
            if isinstance(m, dict) and m.get("type") != "thread_meta"
        ]
        history_messages = messages_from_dict(clean_raw)

        conversation_turn_count = len([
            m for m in history_messages
            if isinstance(m, (HumanMessage, AIMessage))
        ])

        if conversation_turn_count >= 20:
            logger.info(
                f"Thread '{thread_id}' reached {conversation_turn_count} turns. "
                "Triggering automated human escalation."
            )
            escalation_resp = await asyncio.to_thread(
                mark_thread_escalated,
                thread_id,
                user_id,
                "Conversation exceeded 20 message turns limit",
                f"Automated escalation: session reached {conversation_turn_count} turns without final resolution.",
            )

            yield f"data: {json.dumps({'type': 'token', 'content': escalation_resp.message})}\n\n"
            yield f"data: {json.dumps({'type': 'escalation', 'reason': escalation_resp.reason, 'thread_id': thread_id})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'thread_id': thread_id, 'escalated': True})}\n\n"
            return

        agent = create_customer_support_agent(
            user_id=user_id,
            thread_id=thread_id,
            order_id=order_id,
        )

        input_messages = history_messages + [HumanMessage(content=query)]
        accumulated_tokens: List[str] = []
        final_answer = ""
        escalated_during_run = False

        async for event in agent.astream_events({"messages": input_messages}, version="v2"):
            event_type = event.get("event")

            if event_type == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk and chunk.content and not getattr(chunk, "tool_call_chunks", None):
                    if isinstance(chunk.content, str):
                        token_text = chunk.content
                    elif isinstance(chunk.content, list):
                        token_text = "".join(b.get("text", "") for b in chunk.content if isinstance(b, dict))
                    else:
                        token_text = str(chunk.content)

                    if token_text:
                        accumulated_tokens.append(token_text)
                        yield f"data: {json.dumps({'type': 'token', 'content': token_text})}\n\n"

            elif event_type == "on_tool_start":
                tool_name = event.get("name", "tool")
                tool_args = event.get("data", {}).get("input") or {}
                yield f"data: {json.dumps({'type': 'tool_call', 'name': tool_name, 'args': tool_args})}\n\n"

            elif event_type == "on_tool_end":
                tool_name = event.get("name", "tool")
                tool_output = event.get("data", {}).get("output")

                if tool_name == "escalate_to_human":
                    escalated_during_run = True
                    esc_payload = tool_output.model_dump() if hasattr(tool_output, "model_dump") else tool_output
                    msg = esc_payload.get("message") if isinstance(esc_payload, dict) else str(tool_output)
                    reason = esc_payload.get("reason", "Customer support escalation") if isinstance(esc_payload, dict) else "Escalated"
                    final_answer = msg
                    yield f"data: {json.dumps({'type': 'token', 'content': msg})}\n\n"
                    yield f"data: {json.dumps({'type': 'escalation', 'reason': reason, 'thread_id': thread_id})}\n\n"
                else:
                    if hasattr(tool_output, "model_dump_json"):
                        result_str = tool_output.model_dump_json()
                    elif hasattr(tool_output, "model_dump"):
                        result_str = json.dumps(tool_output.model_dump())
                    elif isinstance(tool_output, dict):
                        result_str = json.dumps(tool_output)
                    else:
                        result_str = str(tool_output)

                    yield f"data: {json.dumps({'type': 'tool_result', 'name': tool_name, 'result': result_str})}\n\n"

        if not final_answer:
            final_answer = "".join(accumulated_tokens)

        if not escalated_during_run:
            now_iso = datetime.now(timezone.utc).isoformat()
            updated_messages = history_messages + [
                HumanMessage(content=query),
                AIMessage(content=final_answer),
            ]
            serialized_messages = messages_to_dict(updated_messages)

            def _upsert_conversation():
                try:
                    supabase_client.table("conversations").upsert({
                        "thread_id": thread_id,
                        "user_id": user_id,
                        "order_id": order_id,
                        "all_messages": serialized_messages,
                        "updated_at": now_iso,
                    }).execute()
                except Exception:
                    meta_record = {"type": "thread_meta", "order_id": order_id}
                    supabase_client.table("conversations").upsert({
                        "thread_id": thread_id,
                        "user_id": user_id,
                        "all_messages": [meta_record] + serialized_messages,
                        "updated_at": now_iso,
                    }).execute()

            await asyncio.to_thread(_upsert_conversation)

        yield f"data: {json.dumps({'type': 'done', 'thread_id': thread_id, 'escalated': escalated_during_run})}\n\n"

    except Exception as e:
        logger.exception(f"Unhandled error in chat stream for thread '{thread_id}': {e}")
        yield f"data: {json.dumps({'type': 'error', 'detail': str(e)})}\n\n"


@router.post("/chat/create_thread", response_model=CreateThreadResponse, status_code=status.HTTP_201_CREATED)
@router.post("/chat/thread", response_model=CreateThreadResponse, status_code=status.HTTP_201_CREATED)
async def create_thread(
    req: CreateThreadRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Create a new conversation thread attached to an order.

    Accepts order_id or order_number, identifies the order, and creates a new conversation row.
    """
    user_id = current_user.id
    target_identifier = req.order_number if req.order_number is not None else req.order_id

    if target_identifier is None or not str(target_identifier).strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'order_id' or 'order_number' is required to create a conversation thread.",
        )

    order, err = find_order_with_auth_check(target_identifier, user_id)
    if err or not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=err or f"Order '{target_identifier}' was not found for your account.",
        )

    new_thread_id = str(uuid.uuid4())
    resolved_order_id = str(order["id"])
    resolved_order_number = order["order_number"]
    now_iso = datetime.now(timezone.utc).isoformat()

    def _insert():
        try:
            supabase_client.table("conversations").insert({
                "thread_id": new_thread_id,
                "user_id": user_id,
                "order_id": resolved_order_id,
                "all_messages": [],
                "created_at": now_iso,
                "updated_at": now_iso,
            }).execute()
        except Exception:
            supabase_client.table("conversations").insert({
                "thread_id": new_thread_id,
                "user_id": user_id,
                "all_messages": [{"type": "thread_meta", "order_id": resolved_order_id, "order_number": resolved_order_number}],
                "created_at": now_iso,
                "updated_at": now_iso,
            }).execute()

    await asyncio.to_thread(_insert)

    return CreateThreadResponse(
        thread_id=new_thread_id,
        user_id=user_id,
        order_id=resolved_order_id,
        order_number=resolved_order_number,
        created_at=now_iso,
    )


@router.post("/chat/{thread_id}")
async def chat_endpoint(
    thread_id: str,
    req: ChatRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Chat endpoint supporting real-time Server-Sent Events (SSE) streaming.

    Retrieves the permanently attached order_id for the thread and user from the database.
    """
    user_id = current_user.id

    def _fetch_existing():
        return (
            supabase_client.table("conversations")
            .select("*")
            .eq("thread_id", thread_id)
            .eq("user_id", user_id)
            .execute()
        )

    res = await asyncio.to_thread(_fetch_existing)
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation thread '{thread_id}' not found. Please create a thread first.",
        )

    conv_row = res.data[0]
    attached_order_id = conv_row.get("order_id")
    if not attached_order_id:
        for m in (conv_row.get("all_messages") or []):
            if isinstance(m, dict) and m.get("type") == "thread_meta":
                attached_order_id = m.get("order_id")
                break

    return StreamingResponse(
        _chat_stream_generator(
            user_id=user_id,
            thread_id=thread_id,
            query=req.query,
            order_id=attached_order_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Thread-ID": thread_id,
        },
    )


@router.get("/chat/{thread_id}/history")
async def get_chat_history(
    thread_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Retrieve message history for an existing conversation thread."""
    def _fetch():
        return (
            supabase_client.table("conversations")
            .select("*")
            .eq("thread_id", thread_id)
            .eq("user_id", current_user.id)
            .execute()
        )

    res = await asyncio.to_thread(_fetch)
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation thread '{thread_id}' not found.",
        )

    conv = res.data[0]
    attached_order_id = conv.get("order_id")
    clean_messages = []
    for m in (conv.get("all_messages") or []):
        if isinstance(m, dict) and m.get("type") == "thread_meta":
            if not attached_order_id:
                attached_order_id = m.get("order_id")
        else:
            clean_messages.append(m)

    return {
        "thread_id": conv.get("thread_id"),
        "user_id": conv.get("user_id"),
        "order_id": attached_order_id,
        "messages": clean_messages,
        "is_completed": is_thread_completed(thread_id, current_user.id),
        "updated_at": conv.get("updated_at"),
    }


@router.get("/chat/threads")
async def list_user_threads(
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Retrieve all conversation threads for the authenticated user."""
    def _fetch():
        return (
            supabase_client.table("conversations")
            .select("thread_id, user_id, all_messages, created_at, updated_at")
            .eq("user_id", current_user.id)
            .order("updated_at", desc=True)
            .execute()
        )

    res = await asyncio.to_thread(_fetch)
    threads = []
    for row in (res.data or []):
        order_id = row.get("order_id")
        order_num = None
        msgs = row.get("all_messages") or []
        for m in msgs:
            if isinstance(m, dict) and m.get("type") == "thread_meta":
                if not order_id:
                    order_id = m.get("order_id")
                order_num = m.get("order_number")
                break

        last_preview = ""
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("type") != "thread_meta":
                content = m.get("data", {}).get("content", "")
                if content:
                    last_preview = content[:60]
                    break

        threads.append({
            "thread_id": row.get("thread_id"),
            "order_id": order_id,
            "order_number": order_num,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "last_preview": last_preview,
            "is_completed": is_thread_completed(row.get("thread_id"), current_user.id),
        })

    return threads


@router.get("/orders/my")
async def list_my_orders(
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Retrieve orders belonging to the authenticated user."""
    def _fetch():
        return (
            supabase_client.table("orders")
            .select("id, order_number, status, total, currency, placed_at")
            .eq("customer_id", current_user.id)
            .order("order_number", desc=True)
            .execute()
        )

    res = await asyncio.to_thread(_fetch)
    return res.data or []

