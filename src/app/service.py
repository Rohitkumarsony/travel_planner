import os
from dotenv import load_dotenv
import openai
from typing import Dict, List, Any
from pymongo import MongoClient
from datetime import datetime
import json
import re
from app.prompts import SYSTEM_PROMPT, QUESTION_SET, ITINERARY_AI_MESSAGE,QUERY_PROMPTS
from fastapi import WebSocket
from app.model import ExtractedResponse
load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
MONGODB_URI = os.getenv('MONGODB_URIS')
DB_NAME = os.getenv('DATABASE_NAME') or 'travelliko'
GPT_MODEL = "gpt-4o-mini"  
COLLECTION_NAME = os.getenv('COLLECTION') or 'chat_sessions'
USE_OPENAI = bool(OPENAI_API_KEY)

# Initialize OpenAI API
openai.api_key = OPENAI_API_KEY

# ----------------------
# In-memory DB fallback
# ----------------------
class InMemoryCollection:
    def __init__(self) -> None:
        self._docs: Dict[str, Dict[str, Any]] = {}

    def _match(self, filt: Dict[str, Any]) -> List[Dict[str, Any]]:
        if 'session_id' in filt:
            doc = self._docs.get(filt['session_id'])
            return [doc] if doc else []
        # simple fallback: linear scan
        results = []
        for d in self._docs.values():
            ok = True
            for k, v in filt.items():
                if d.get(k) != v:
                    ok = False
                    break
            if ok:
                results.append(d)
        return results

    def find_one(self, filt: Dict[str, Any], projection: Dict[str, int] = None) -> Dict[str, Any]:
        matches = self._match(filt)
        if not matches:
            return None
        doc = matches[0]
        if projection:
            projected = {}
            for key, inc in projection.items():
                if inc and key in doc:
                    projected[key] = doc[key]
                elif '.' in key:
                    # support simple nested projection like latest_analysis.complete
                    top, sub = key.split('.', 1)
                    if top in doc and isinstance(doc[top], dict) and inc:
                        if top not in projected:
                            projected[top] = {}
                        projected[top][sub] = doc[top].get(sub)
            return projected
        return doc

    def insert_one(self, doc: Dict[str, Any]):
        sid = doc.get('session_id') or str(len(self._docs) + 1)
        doc['session_id'] = sid
        self._docs[sid] = doc
        return {'inserted_id': sid}

    def update_one(self, filt: Dict[str, Any], update: Dict[str, Any], upsert: bool = False):
        existing = self.find_one(filt)
        if not existing and upsert:
            existing = {}
            if 'session_id' in filt:
                existing['session_id'] = filt['session_id']
            existing.setdefault('messages', [])
            self._docs[existing['session_id']] = existing
        if not existing:
            return {'matched_count': 0}

        if '$push' in update:
            for arr_key, arr_val in update['$push'].items():
                if isinstance(arr_val, dict) and '$each' in arr_val:
                    existing.setdefault(arr_key, [])
                    existing[arr_key].extend(arr_val['$each'])
                else:
                    existing.setdefault(arr_key, [])
                    existing[arr_key].append(arr_val)

        if '$set' in update:
            for k, v in update['$set'].items():
                # support simple nested set latest_analysis.*
                if '.' in k:
                    top, sub = k.split('.', 1)
                    existing.setdefault(top, {})
                    if isinstance(existing[top], dict):
                        existing[top][sub] = v
                    else:
                        existing[top] = {sub: v}
                else:
                    existing[k] = v

        self._docs[existing['session_id']] = existing
        return {'matched_count': 1}


class InMemoryDB:
    def __init__(self) -> None:
        self._collections: Dict[str, InMemoryCollection] = {}

    def __getitem__(self, name: str) -> InMemoryCollection:
        if name not in self._collections:
            self._collections[name] = InMemoryCollection()
        return self._collections[name]

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.session_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        self.active_connections.append(websocket)
        self.session_connections.setdefault(session_id, []).append(websocket)

    def disconnect(self, websocket: WebSocket, session_id: str):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        if session_id in self.session_connections and websocket in self.session_connections[session_id]:
            self.session_connections[session_id].remove(websocket)
            # Clean up empty session lists to prevent memory leaks
            if not self.session_connections[session_id]:
                del self.session_connections[session_id]

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        await websocket.send_json(message)

    async def broadcast_to_session(self, session_id: str, message: dict):
        if session_id in self.session_connections:
            for conn in self.session_connections[session_id]:
                try:
                    await conn.send_json(message)
                except Exception as e:
                    print(f"Error sending message to client: {str(e)}")
                    # Don't disconnect here, just log the error

manager = ConnectionManager()

# Prefer real Mongo if configuration exists; otherwise fallback to in-memory
if MONGODB_URI:
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=500)
        # Validate connectivity; if it fails, fallback to in-memory
        try:
            client.admin.command("ping")
            db = client[DB_NAME]
        except Exception:
            db = InMemoryDB()
        chat_sessions = db[COLLECTION_NAME]
    except Exception:
        db = InMemoryDB()
        chat_sessions = db[COLLECTION_NAME]
else:
    db = InMemoryDB()
    chat_sessions = db[COLLECTION_NAME]

# List of common greetings for natural language detection
GREETING_PATTERNS = [
    r'\b(hi|hello|hey|greetings|howdy|hola|namaste|sup|yo|good morning|good afternoon|good evening)\b',
    r'\b(what\'?s up|how are you|how\'?s it going|how do you do)\b'
]

def get_db():
    return db

# Helper function to format chat history
def format_chat_history(messages: List[Dict]) -> str:
    formatted_history = ""
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        formatted_history += f"{role.upper()}: {content}\n\n"
    return formatted_history

# Helper function to check if a message is primarily a greeting
def is_greeting(message: str) -> bool:
    message = message.lower().strip()
    
    for pattern in GREETING_PATTERNS:
        if re.search(pattern, message):
            # If the message is short and just a greeting
            if len(message.split()) <= 5:
                return True
    
    return False

async def analyze_message(message: str, history: List[Dict]) -> Dict:
    formatted_history = format_chat_history(history)
    
    system_content = SYSTEM_PROMPT.format(
        question_set=json.dumps(QUESTION_SET, indent=2),
        chat_history=formatted_history
    )
    
    if USE_OPENAI:
        try:
            response = openai.ChatCompletion.create(
                model=GPT_MODEL,  # Using the consistent model variable
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": message}
                ],
                temperature=0.5
            )
            assistant_response = response['choices'][0]['message']['content']
            try:
                parsed_response = json.loads(assistant_response)
                return parsed_response
            except json.JSONDecodeError:
                return {
                    "response": assistant_response,
                    "missing_fields": list(QUESTION_SET.keys()),
                    "complete": False,
                    "extracted_data": {}
                }
        except Exception as e:
            print(f"Error in analyze_message: {str(e)}")
            return {
                "response": f"I'm having trouble processing your request. Please try again.",
                "missing_fields": list(QUESTION_SET.keys()),
                "complete": False,
                "extracted_data": {}
            }
    else:
        # Offline heuristic: ask for the first missing field
        asked_fields = set()
        for msg in history:
            try:
                data = json.loads(msg.get('content', '{}'))
                if isinstance(data, dict) and 'extracted_data' in data:
                    asked_fields.update(k for k, v in data['extracted_data'].items() if v)
            except Exception:
                pass
        missing = [k for k in QUESTION_SET.keys() if k not in asked_fields]
        next_q = QUESTION_SET[missing[0]] if missing else "Thanks! Generating your itinerary..."
        return {
            "response": next_q,
            "missing_fields": missing,
            "complete": False,
            "extracted_data": {}
        }

# Async helper function to analyze messages and generate itinerary plan
async def analyze_message_for_itinerary(data: Dict) -> Dict:
    if USE_OPENAI:
        try:
            system_content = ITINERARY_AI_MESSAGE.format(**data["extracted_data"])
            response = openai.ChatCompletion.create(
                model=GPT_MODEL,  
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": "Please generate the itinerary based on the preferences above."}
                ],
                temperature=0.2
            )
            assistant_response = response['choices'][0]['message']['content']
            return {"itinerary": assistant_response}
        except Exception as e:
            print(f"Error in analyze_message_for_itinerary: {str(e)}")
            return {"status_code": 500, "message": f"An error occurred while generating itinerary: {str(e)}"}
    else:
        return {"itinerary": "[Offline mode] Itinerary generation is disabled without OPENAI_API_KEY."}

def format_travel_data_lines(json_data: dict) -> List[str]:
    data = ExtractedResponse(**json_data).extracted_data
    return [
        f"Destination: {data.destination}",
        f"Departure: {data.departure}",
        f"Travel duration: {data.travel_period}",
        f"Number of travelers: {data.people}",
        f"Budget: {data.budget}",
        f"Accommodation: {data.accommodation}",
        f"Activities: {', '.join(set(data.activities))}",
        f"Food preferences: {data.food}"
    ]



# Check if the conversation is complete
def is_user_input_complete(session_id: str) -> bool:
    result = db[COLLECTION_NAME].find_one({"session_id": session_id}, {"latest_analysis.complete": 1})
    return result.get("latest_analysis", {}).get("complete", False) if result else False

# Analyze user query to determine update status
def analyze_user_query(session_id: str, user_input: str) -> Dict:
    messages = [
        {"role": "system", "content": QUERY_PROMPTS},
        {"role": "user", "content": user_input}
    ]

    if USE_OPENAI:
        try:
            response = openai.ChatCompletion.create(
                model=GPT_MODEL,  
                messages=messages,
                temperature=0,
                max_tokens=50
            )
            result_text = response['choices'][0]['message']['content']
            try:
                result_json = json.loads(result_text)
                if result_json.get("updated_status") in ["YES", "NO"]:
                    result_json["complete"] = False
                else:
                    session_data = db[COLLECTION_NAME].find_one(
                        {"session_id": session_id}, {"latest_analysis.complete": 1}
                    )
                    previous_complete = session_data.get("latest_analysis", {}).get("complete", False) if session_data else False
                    result_json["complete"] = previous_complete
                now_str = datetime.now().isoformat()
                current_message = {"role": "user", "content": user_input, "timestamp": now_str}
                assistant_message = {"role": "assistant", "content": result_text, "timestamp": now_str}
                db[COLLECTION_NAME].update_one(
                    {"session_id": session_id},
                    {
                        "$push": {"messages": {"$each": [current_message, assistant_message]}},
                        "$set": {"updated_at": datetime.now(), "latest_analysis": {"complete": result_json.get("complete", False), "status": result_json.get("status"), "updated_status": result_json.get("updated_status"),}}
                    }
                )
                return result_json
            except json.JSONDecodeError:
                print(f"JSON decode error for result: {result_text}")
                return {"status": "normal_query", "updated_status": "NO", "complete": False}
        except Exception as e:
            print(f"OpenAI error in analyze_user_query: {e}")
            return {"status": "normal_query", "updated_status": "NO", "complete": False}
    else:
        # Offline classification: treat as normal query
        return {"status": "normal_query", "updated_status": "NO", "complete": False}