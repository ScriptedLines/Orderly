from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    vector_store_connected: bool


class CreateThreadRequest(BaseModel):
    order_id: Optional[str] = None
    order_number: Optional[Union[int, str]] = None


class CreateThreadResponse(BaseModel):
    thread_id: str
    user_id: str
    order_id: str
    order_number: int
    created_at: str


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)


class SourceCitation(BaseModel):
    source: str
    chunk_excerpt: str


class ChatResponse(BaseModel):
    answer: str
    sources: List[SourceCitation] = Field(default_factory=list)


class IngestResponse(BaseModel):
    message: str
    total_chunks: int
    files_processed: List[str]


class OrderItemData(BaseModel):
    product_id: Optional[str] = None
    product_name: str
    category: Optional[str] = ""
    quantity: int
    unit_price: float
    subtotal: float
    is_final_sale: bool = False
    description: Optional[str] = ""
    specs: Dict[str, Any] = Field(default_factory=dict)


class ShipmentData(BaseModel):
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    shipped_at: Optional[str] = None
    expected_delivery: Optional[str] = None
    last_status: Optional[str] = None
    updated_at: Optional[str] = None


class OrderData(BaseModel):
    order_number: int
    order_id: str
    status: str
    placed_at: str
    delivered_at: Optional[str] = None
    total: float
    currency: str = "INR"
    item_count: int
    items: List[OrderItemData] = Field(default_factory=list)
    shipments: List[ShipmentData] = Field(default_factory=list)


class OrderSummaryData(BaseModel):
    order_number: int
    order_id: str
    status: str
    placed_at: str
    delivered_at: Optional[str] = None
    total: float
    currency: str = "INR"
    items: List[str] = Field(default_factory=list)


class ItemReturnEligibility(BaseModel):
    product_name: str
    quantity: int = 1
    is_final_sale: bool = False
    returnable: bool = True
    note: str


class OrderLookupInput(BaseModel):
    pass


class OrderDetailsResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    data: Optional[OrderData] = None


class ListCustomerOrdersInput(BaseModel):
    limit: int = Field(default=5, description="Maximum number of recent orders to retrieve.")


class ListCustomerOrdersResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    message: Optional[str] = None
    count: int = 0
    orders: List[OrderSummaryData] = Field(default_factory=list)


class CancelOrderInput(BaseModel):
    reason: Optional[str] = Field(default="", description="Optional cancellation reason provided by the customer.")


class CancelOrderResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    message: str
    order_number: Optional[int] = None
    previous_status: Optional[str] = None
    current_status: Optional[str] = None
    reason: Optional[str] = None


class CheckReturnEligibilityInput(BaseModel):
    pass


class CheckReturnEligibilityResponse(BaseModel):
    eligible: bool
    error: Optional[str] = None
    order_number: Optional[int] = None
    order_status: Optional[str] = None
    delivered_at: Optional[str] = None
    days_since_delivery: Optional[int] = None
    message: str
    items: List[ItemReturnEligibility] = Field(default_factory=list)


class PolicySearchInput(BaseModel):
    query: str = Field(..., description="Question or search query regarding orders, shipping, return, refund, cancellation, or store policies.")


class EscalateToHumanInput(BaseModel):
    reason: str = Field(..., description="The reason for escalating to a human agent (e.g. 'Customer requested human agent', 'Complex dispute', 'Damaged goods claim').")
    summary: Optional[str] = Field(default="", description="A concise summary of the conversation issue for the human support agent.")


class EscalateToHumanResponse(BaseModel):
    success: bool
    escalated: bool = True
    thread_completed: bool = True
    thread_id: str
    message: str
    reason: str
