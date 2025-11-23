from pydantic import BaseModel
from typing import List
from datetime import datetime

# Request models
class ChatRequest(BaseModel):
    message: str

class SessionCreateResponse(BaseModel):
    session_id: str
    created_at: datetime

class ChatResponse(BaseModel):
    response: str

class TravelData(BaseModel):
    destination: str
    departure: str
    travel_period: str
    people: str
    budget: str
    accommodation: str
    food: str
    activities: List[str]

class ExtractedResponse(BaseModel):
    extracted_data: TravelData
