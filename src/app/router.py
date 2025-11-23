from fastapi import APIRouter, HTTPException, Depends, Response
import openai
from typing import Dict, Union
import uuid
from datetime import datetime
import json
import re
from app.prompts import SYSTEM_PROMPT, QUESTION_SET, ITINERARY_AI_MESSAGE,INTRODUCTION
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.service import get_db,COLLECTION_NAME,analyze_message,analyze_message_for_itinerary,analyze_user_query,manager,is_user_input_complete,format_chat_history,GPT_MODEL
import asyncio
from app.model import SessionCreateResponse,ChatRequest,ChatResponse

chat_router=APIRouter()

# Create a new chat session
@chat_router.get("/sessions", response_model=SessionCreateResponse)
async def create_session(db=Depends(get_db)):
    session_id = str(uuid.uuid4())
    now = datetime.now()
    
    session_data = {
        "session_id": session_id,
        "created_at": now,
        "updated_at": now,
        "status": "active",
        "messages": [],  # Initialize empty messages array
        "latest_analysis": {
            "missing_fields": list(QUESTION_SET.keys()),
            "complete": False,
            "extracted_data": {}
        }
    }
    
    db[COLLECTION_NAME].insert_one(session_data)
    
    return SessionCreateResponse(
        session_id=session_id,
        created_at=now
    )

# Send a message in an existing chat session
@chat_router.post("/sessions/{session_id}/chat", response_model=Union[ChatResponse, Dict],include_in_schema=False)
async def send_message(session_id: str, request: ChatRequest, db=Depends(get_db)):
    # Verify session exists
    session = db[COLLECTION_NAME].find_one({"session_id": session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    history = session.get("messages", [])
    current_message = {"role": "user", "content": request.message, "timestamp": datetime.now()}
    analysis_history = history + [current_message]
    analysis_result = await analyze_message(request.message, analysis_history)
    combined_data = session.get("latest_analysis", {}).get("extracted_data", {}).copy()
    
    if analysis_result.get("extracted_data"):
        if "activities" in analysis_result.get("extracted_data", {}) and "activities" in combined_data:
            existing_activities = combined_data.get("activities", [])
            new_activities = analysis_result["extracted_data"]["activities"]
            all_activities = list(set(existing_activities + new_activities))
            combined_data["activities"] = all_activities
            del analysis_result["extracted_data"]["activities"]
        
        combined_data.update(analysis_result.get("extracted_data", {}))
    
    missing_fields = []
    for field in QUESTION_SET.keys():
        if field not in combined_data or not combined_data[field]:
            missing_fields.append(field)
    
    analysis_result["extracted_data"] = combined_data
    analysis_result["missing_fields"] = missing_fields
    analysis_result["complete"] = len(missing_fields) == 0
    
    assistant_message = {
        "role": "assistant", 
        "content": analysis_result["response"], 
        "timestamp": datetime.now()
    }
    
    db[COLLECTION_NAME].update_one(
        {"session_id": session_id},
        {
            "$push": {
                "messages": {
                    "$each": [current_message, assistant_message]
                }
            },
            "$set": {
                "updated_at": datetime.now(),
                "latest_analysis": {
                    "missing_fields": analysis_result.get("missing_fields", []),
                    "complete": analysis_result.get("complete", False),
                    "extracted_data": analysis_result.get("extracted_data", {})
                }
            }
        }
    )
    
    if analysis_result.get("complete", False):
        extracted = analysis_result.get("extracted_data", {})
        itinerary_response = await analyze_message_for_itinerary({
            "extracted_data": extracted
        })

        return Response(
            content=json.dumps({
                "itinerary": itinerary_response.get("itinerary", "Itinerary could not be generated.")
            }),
            media_type="application/json"
        )

    return ChatResponse(
        response=analysis_result["response"]
    )

def _to_isoformat(ts):
    try:
        return ts.isoformat()
    except Exception:
        return ts

# Get chat history for a session
@chat_router.get("/sessions/{session_id}/history")
async def get_chat_history(session_id: str, db=Depends(get_db)):
    session = db[COLLECTION_NAME].find_one({"session_id": session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    messages = session.get("messages", [])
    
    formatted_messages = []
    for msg in messages:
        formatted_msg = msg.copy()
        if "timestamp" in formatted_msg:
            formatted_msg["timestamp"] = _to_isoformat(formatted_msg["timestamp"])
        formatted_messages.append(formatted_msg)
    
    return {"session_id": session_id, "messages": formatted_messages}

# Get analytics for a session
@chat_router.get("/sessions/{session_id}/analytics",include_in_schema=False)
async def get_session_analytics(session_id: str, db=Depends(get_db)):
    session = db[COLLECTION_NAME].find_one({"session_id": session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    latest_analysis = session.get("latest_analysis", {})
    
    # Format messages for transcript
    messages = session.get("messages", [])
    formatted_transcript = []
    for msg in messages:
        formatted_msg = {
            "role": msg.get("role"),
            "content": msg.get("content"),
            "timestamp": _to_isoformat(msg.get("timestamp")) if "timestamp" in msg else None
        }
        formatted_transcript.append(formatted_msg)
    
    return {
        "session_id": session_id,
        "message_count": len(session.get("messages", [])),
        "latest_analysis": latest_analysis,
        "completion_status": latest_analysis.get("complete", False),
        "collected_data": latest_analysis.get("extracted_data", {}),
        "full_transcript": formatted_transcript
    }

# Get completion status with extracted data (for direct API consumption)
@chat_router.get("/sessions/{session_id}/completion",include_in_schema=False)
async def get_completion_data(session_id: str, db=Depends(get_db)):
    session = db[COLLECTION_NAME].find_one({"session_id": session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    latest_analysis = session.get("latest_analysis", {})
    
    if latest_analysis.get("complete", False):
        return {"extracted_data": latest_analysis.get("extracted_data", {})}
    else:
        # Return status with missing fields
        return {
            "complete": False,
            "missing_fields": latest_analysis.get("missing_fields", [])
        }



# @chat_router.websocket("/ws/{session_id}")
# async def websocket_endpoint(websocket: WebSocket, session_id: str, db=Depends(get_db)):
#     await manager.connect(websocket, session_id)

#     try:
#         session = db[COLLECTION_NAME].find_one({"session_id": session_id})
#         if not session:
#             # Create a new session if it doesn't exist
#             db[COLLECTION_NAME].insert_one({
#                 "session_id": session_id, 
#                 "messages": [],
#                 "created_at": datetime.now(),
#                 "updated_at": datetime.now(),
#                 "latest_analysis": {
#                     "extracted_data": {},
#                     "missing_fields": list(QUESTION_SET.keys()),
#                     "complete": False
#                 }
#             })
            
#             session = db[COLLECTION_NAME].find_one({"session_id": session_id})
        
#         history = session.get("messages", [])
#         latest = session.get("latest_analysis", {})

#         await manager.send_personal_message({"type": "history", "data": history}, websocket)
#         await manager.send_personal_message({
#             "type": "extraction_data",
#             "data": {
#                 "extracted_data": latest.get("extracted_data", {}),
#                 "missing_fields": latest.get("missing_fields", list(QUESTION_SET.keys())),
#                 "complete": latest.get("complete", False)
#             }
#         }, websocket)

#         # Always stream the welcome message when connection is established
#         await manager.broadcast_to_session(session_id, {"type": "typing", "data": {"status": True}})
        
#         for char in INTRODUCTION:
#             await manager.broadcast_to_session(session_id, {
#                 "type": "stream_chunk",
#                 "data": {"content": char}
#             })
#             await asyncio.sleep(0.01) 
        
#         # Send a message complete event
#         welcome_msg = {
#             "role": "assistant",
#             "content": INTRODUCTION,
#             "timestamp": datetime.now().isoformat()
#         }
        
#         if not history:
#             db[COLLECTION_NAME].update_one(
#                 {"session_id": session_id},
#                 {"$push": {"messages": welcome_msg}}
#             )
        
#         await manager.broadcast_to_session(session_id, {
#             "type": "message_complete",
#             "data": {"message": welcome_msg}
#         })
        
#         # Hide typing indicator
#         await manager.broadcast_to_session(session_id, {"type": "typing", "data": {"status": False}})

#         while True:
#             try:
#                 data = json.loads(await websocket.receive_text())
#             except WebSocketDisconnect:
#                 manager.disconnect(websocket, session_id)
#                 break
#             except Exception as e:
#                 print(f"Error receiving message: {str(e)}")
#                 continue

#             if data["type"] == "message":
#                 user_msg = {
#                     "role": "user",
#                     "content": data["content"],
#                     "timestamp": datetime.now().isoformat()
#                 }

#                 user_input = data["content"].lower()
#                 is_complete = is_user_input_complete(session_id)
#                # The above code is checking if the variable `is_complete` is True. If it is True, then
#                # the code inside the if block (indicated by the `
#                 if is_complete:
#                     update_status = analyze_user_query(session_id, user_input)
#                     # print('Update status:', update_status)

#                 is_update = any(keyword in user_input for keyword in 
#                                ["update", "change", "modify", "instead of", "replace", "only", "just"])
                
#                 update_field_indicators = {
#                     "travel_period": ["day", "days", "duration", "period", "length", "time"],
#                     "budget": ["budget", "cost", "money", "spend", "rupees", "dollars"],
#                     "people": ["people", "travelers", "guests", "persons", "group"],
#                     "accommodation": ["hotel", "resort", "stay", "accommodation", "room"],
#                     "destination": ["destination", "place", "location", "city", "country"],
#                     "activities": ["activity", "activities", "do", "visit", "see", "experience"],
#                     "food": ["food", "meal", "eat", "cuisine", "restaurant", "diet"]
#                 }
                
#                 for field, indicators in update_field_indicators.items():
#                     if any(indicator in user_input for indicator in indicators):
#                         is_update = True
#                         # print(f"Detected potential update for field: {field}")

#                 # print(f"Is update request: {is_update}")

#                 db[COLLECTION_NAME].update_one(
#                     {"session_id": session_id},
#                     {"$push": {"messages": user_msg}, "$set": {"updated_at": datetime.now()}}
#                 )

#                 await manager.broadcast_to_session(session_id, {
#                     "type": "message",
#                     "data": {"message": user_msg}
#                 })
#                 await manager.broadcast_to_session(session_id, {"type": "typing", "data": {"status": True}})

#                 session = db[COLLECTION_NAME].find_one({"session_id": session_id})
#                 if not session:
#                     await manager.send_personal_message({"type": "error", "data": {"message": "Session lost"}}, websocket)
#                     continue

#                 history = session.get("messages", [])
#                 combined_data = session.get("latest_analysis", {}).get("extracted_data", {}).copy()
                
#                 # print(f"Session {session_id} - Current data before update: {combined_data}")

#                 update_instruction = ""
#                 if is_update:
#                     update_instruction = """
#                     IMPORTANT: This is an update request. The user wants to CHANGE one or more fields they provided earlier.
#                     Carefully identify what fields are being updated and extract the new values.
#                     For example, if they say "change my trip to 9 days", extract travel_period as "9 days".
#                     Only include fields that are being updated in your JSON response.
#                     """
                
#                 messages = [
#                     {"role": "system", "content": SYSTEM_PROMPT.format(
#                         question_set=json.dumps(QUESTION_SET, indent=2),
#                         chat_history=format_chat_history(history)
#                     ) + "\nIMPORTANT: Return ONLY the text response without any JSON structure or metadata. Do not include any JSON in your response."},
#                     {"role": "user", "content": user_msg["content"]}
#                 ]

#                 try:
#                     stream = openai.ChatCompletion.create(
#                         model=GPT_MODEL,  
#                         messages=messages,
#                         temperature=0.5,
#                         stream=True
#                     )

#                     full_response = ""
#                     for chunk in stream:
#                         if chunk.choices[0].delta.get("content"):
#                             token = chunk.choices[0].delta.content
#                             full_response += token
#                             await manager.broadcast_to_session(session_id, {
#                                 "type": "stream_chunk",
#                                 "data": {"content": token}
#                             })
#                             await asyncio.sleep(0.01)
#                 except Exception as e:
#                     print(f"Error in streaming response: {str(e)}")
#                     await manager.broadcast_to_session(session_id, {
#                         "type": "error", 
#                         "data": {"message": f"Error generating response: {str(e)}"}
#                     })
#                     continue

#                 try:
#                     analysis_messages = [
#                         {"role": "system", "content": SYSTEM_PROMPT.format(
#                             question_set=json.dumps(QUESTION_SET, indent=2),
#                             chat_history=format_chat_history(history)
#                         ) + update_instruction},
#                         {"role": "user", "content": user_msg["content"]}
#                     ]
                    
#                     if is_update:
#                         analysis_messages.append({
#                             "role": "system", 
#                             "content": f"""
#                             Remember, this is an UPDATE request. The user wants to change something in their existing data.
#                             Current data: {json.dumps(combined_data, indent=2)}
                            
#                             Extract ONLY the fields being updated. Your response should be valid JSON with ONLY the updated fields.
#                             Example format: {{"travel_period": "9 days"}}
#                             """
#                         })
                    
#                     analysis_response = openai.ChatCompletion.create(
#                         model=GPT_MODEL,  
#                         messages=analysis_messages,
#                         temperature=0.5
#                     )
                    
#                     analysis_text = analysis_response.choices[0].message.content
#                     # print(f"Raw analysis response: {analysis_text}")
                    
#                     analysis = None
#                     try:
#                         analysis = json.loads(analysis_text)
#                     except json.JSONDecodeError:
#                         json_pattern = r'```json\s*([\s\S]*?)\s*```|{\s*"[^"]+"\s*:|{\s*\'[^\']+\'\s*:'
#                         json_match = re.search(json_pattern, analysis_text)
                        
#                         if json_match:
#                             json_str = json_match.group(1) if json_match.group(1) else analysis_text
#                             json_str = re.sub(r'```json|```', '', json_str).strip()
#                             try:
#                                 analysis = json.loads(json_str)
#                             except json.JSONDecodeError:
#                                 analysis = None
                    
#                     if not analysis:
#                         analysis = {
#                             "response": full_response, 
#                             "extracted_data": {}, 
#                             "missing_fields": list(QUESTION_SET.keys()), 
#                             "complete": False
#                         }
                        
#                     if "extracted_data" not in analysis and "response" not in analysis:
#                         analysis = {
#                             "response": full_response,
#                             "extracted_data": analysis,
#                             "missing_fields": [],
#                             "complete": False
#                         }
                        
#                 except Exception as e:
#                     analysis = {
#                         "response": full_response, 
#                         "extracted_data": {}, 
#                         "missing_fields": list(QUESTION_SET.keys()), 
#                         "complete": False
#                     }
#                     print(f"Error analyzing response: {str(e)}")
#                     await manager.broadcast_to_session(session_id, {
#                         "type": "error", 
#                         "data": {"message": f"Error analyzing response: {str(e)}"}
#                     })

#                 new_data = {}
#                 if "extracted_data" in analysis:
#                     new_data = analysis.get("extracted_data", {})
#                 else:
#                     new_data = analysis
                    
#                 if isinstance(new_data, str):
#                     try:
#                         new_data = json.loads(new_data)
#                     except:
#                         new_data = {}
                
#                 # print(f"Session {session_id} update - New data: {new_data}, Combined before: {combined_data}")
                
#                 for key, val in new_data.items():
#                     if val is not None and val != "": 
#                         if key == "activities":
#                             current_activities = combined_data.get(key, [])
#                             if not isinstance(current_activities, list):
#                                 current_activities = []
                            
#                             if isinstance(val, list):
#                                 new_activities = val
#                             elif isinstance(val, str):
#                                 new_activities = [val]  
#                             else:
#                                 new_activities = []
                                
#                             if is_update and len(new_activities) <= 2:
#                                 activity_update_words = ["add", "include", "also"]
#                                 is_addition = any(word in user_msg["content"].lower() for word in activity_update_words)
                                
#                                 if is_addition:
#                                     combined_data[key] = list(set(current_activities + new_activities))
#                                 else:
#                                     combined_data[key] = new_activities
#                             else:
#                                 combined_data[key] = list(set(current_activities + new_activities))
#                         else:
#                             combined_data[key] = val
                
#                 # print(f"Session {session_id} update - Combined after: {combined_data}")

#                 missing = [k for k in QUESTION_SET.keys() if not combined_data.get(k)]
#                 complete = len(missing) == 0

#                 assistant_msg = {
#                     "role": "assistant",
#                     "content": full_response,
#                     "timestamp": datetime.now().isoformat()
#                 }

#                 # Update database with consistent collection name
#                 try:
#                     db[COLLECTION_NAME].update_one(
#                         {"session_id": session_id},
#                         {
#                             "$push": {"messages": assistant_msg},
#                             "$set": {
#                                 "updated_at": datetime.now(),
#                                 "latest_analysis": {
#                                     "missing_fields": missing,
#                                     "complete": complete,
#                                     "extracted_data": combined_data
#                                 }
#                             }
#                         },
#                         upsert=True  # Add upsert to create document if it doesn't exist
#                     )
#                 except Exception as e:
#                     print(f"Database update error: {str(e)}")
#                     await manager.broadcast_to_session(session_id, {
#                         "type": "error", 
#                         "data": {"message": f"Database error: {str(e)}"}
#                     })
                    
#                 await manager.broadcast_to_session(session_id, {
#                     "type": "extraction_update",
#                     "data": {
#                         "extracted_data": combined_data,
#                         "missing_fields": missing,
#                         "complete": complete
#                     }
#                 })

#                 await manager.broadcast_to_session(session_id, {
#                     "type": "message_complete",
#                     "data": {"message": assistant_msg}
#                 })

#                 if complete:
#                     await manager.broadcast_to_session(session_id, {
#                         "type": "itinerary_status",
#                         "data": {"status": "generating"}
#                     })

#                     try:
#                         system_msg = ITINERARY_AI_MESSAGE.format(**combined_data)
#                         itinerary_stream = openai.ChatCompletion.create(
#                             model=GPT_MODEL, 
#                             messages=[
#                                 {"role": "system", "content": system_msg},
#                                 {"role": "user", "content": "Please generate the itinerary based on given data. Generate itinerary without repetition."}
#                             ],
#                             temperature=0.3,
#                             stream=True
#                         )

#                         full_itinerary = ""
#                         for chunk in itinerary_stream:
#                             if chunk.choices[0].delta.get("content"):
#                                 token = chunk.choices[0].delta.content
#                                 full_itinerary += token
#                                 await manager.broadcast_to_session(session_id, {
#                                     "type": "itinerary_chunk",
#                                     "data": {"content": token}
#                                 })
#                                 await asyncio.sleep(0.01)
                                
#                         # Store the itinerary
#                         try:
#                             db[COLLECTION_NAME].update_one(
#                                 {"session_id": session_id},
#                                 {
#                                     "$set": {
#                                         "itinerary": full_itinerary,
#                                         "itinerary_generated_at": datetime.now().isoformat()
#                                     }
#                                 }
#                             )
#                         except Exception as db_err:
#                             print(f"Failed to save itinerary: {db_err}")
                            
#                         await manager.broadcast_to_session(session_id, {
#                             "type": "itinerary_complete",
#                             "data": {"itinerary": full_itinerary}
#                         })
#                     except Exception as e:
#                         print(f"Error generating itinerary: {str(e)}")
#                         await manager.broadcast_to_session(session_id, {
#                             "type": "error", 
#                             "data": {"message": f"Itinerary generation error: {str(e)}"}
#                         })

#             await asyncio.sleep(0.01)

#     except WebSocketDisconnect:
#         manager.disconnect(websocket, session_id)
#         print(f"WebSocket disconnected for session {session_id}")
#     except Exception as e:
#         print(f"WebSocket error: {e}")
#         try:
#             await manager.broadcast_to_session(session_id, {
#                 "type": "error",
#                 "data": {"message": f"Connection error: {str(e)}"}
#             })
#         except:
#             pass
#         manager.disconnect(websocket, session_id)



@chat_router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str, db=Depends(get_db)):
    await manager.connect(websocket, session_id)

    try:
        # Check if session exists, create if it doesn't
        session = db[COLLECTION_NAME].find_one({"session_id": session_id})
        if not session:
            # Create a new session
            db[COLLECTION_NAME].insert_one({
                "session_id": session_id, 
                "messages": [],
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
                "latest_analysis": {
                    "extracted_data": {},
                    "missing_fields": list(QUESTION_SET.keys()),
                    "complete": False
                }
            })
            session = db[COLLECTION_NAME].find_one({"session_id": session_id})
        
        history = session.get("messages", [])
        latest = session.get("latest_analysis", {})

        # Send history and extraction data
        await manager.send_personal_message({"type": "history", "data": history}, websocket)
        await manager.send_personal_message({
            "type": "extraction_data",
            "data": {
                "extracted_data": latest.get("extracted_data", {}),
                "missing_fields": latest.get("missing_fields", list(QUESTION_SET.keys())),
                "complete": latest.get("complete", False)
            }
        }, websocket)

        # Always stream the welcome message when connection is established
        await manager.broadcast_to_session(session_id, {"type": "typing", "data": {"status": True}})
        
        try:
            for char in INTRODUCTION:
                # Check if websocket is still connected before sending
                if websocket.client_state.name != "CONNECTED":
                    break
                await manager.broadcast_to_session(session_id, {
                    "type": "stream_chunk",
                    "data": {"content": char}
                })
                await asyncio.sleep(0.01) 
        except Exception as e:
            print(f"Error streaming welcome message: {str(e)}")
            # Continue even if streaming fails
        
        # Send a message complete event
        welcome_msg = {
            "role": "assistant",
            "content": INTRODUCTION,
            "timestamp": datetime.now().isoformat()
        }
        
        # Only add welcome message if it's not already in history
        if not history or (history and history[-1].get("content") != INTRODUCTION):
            try:
                db[COLLECTION_NAME].update_one(
                    {"session_id": session_id},
                    {"$push": {"messages": welcome_msg}}
                )
            except Exception as e:
                print(f"Error saving welcome message: {str(e)}")
        
        await manager.broadcast_to_session(session_id, {
            "type": "message_complete",
            "data": {"message": welcome_msg}
        })
        
        # Hide typing indicator
        await manager.broadcast_to_session(session_id, {"type": "typing", "data": {"status": False}})

        while True:
            try:
                # Check if websocket is still connected
                if websocket.client_state.name != "CONNECTED":
                    print(f"WebSocket not connected for session {session_id}, breaking loop")
                    break
                
                # Use a timeout to avoid hanging on receive_text()
                try:
                    message = await asyncio.wait_for(websocket.receive_text(), timeout=0.1)
                    data = json.loads(message)
                except asyncio.TimeoutError:
                    # No message received, continue loop
                    await asyncio.sleep(0.01)
                    continue
                except RuntimeError as e:
                    if "WebSocket is not connected" in str(e) or "close" in str(e).lower():
                        print(f"WebSocket connection lost for session {session_id}: {e}")
                        break
                    else:
                        print(f"Runtime error for session {session_id}: {e}")
                        continue
                    
            except WebSocketDisconnect:
                print(f"WebSocket disconnected for session {session_id}")
                break
            except ConnectionAbortedError:
                print(f"Connection aborted for session {session_id}")
                break
            except ConnectionResetError:
                print(f"Connection reset for session {session_id}")
                break
            except Exception as e:
                if "WebSocket is not connected" in str(e) or "close" in str(e).lower():
                    print(f"WebSocket connection error for session {session_id}: {e}")
                    break
                print(f"Error receiving message for session {session_id}: {str(e)}")
                continue

            if data["type"] == "message":
                try:
                    user_msg = {
                        "role": "user",
                        "content": data["content"],
                        "timestamp": datetime.now().isoformat()
                    }

                    user_input = data["content"].lower()
                    is_complete = is_user_input_complete(session_id)
                    
                    if is_complete:
                        update_status = analyze_user_query(session_id, user_input)

                    is_update = any(keyword in user_input for keyword in 
                                   ["update", "change", "modify", "instead of", "replace", "only", "just"])
                    
                    update_field_indicators = {
                        "travel_period": ["day", "days", "duration", "period", "length", "time"],
                        "budget": ["budget", "cost", "money", "spend", "rupees", "dollars"],
                        "people": ["people", "travelers", "guests", "persons", "group"],
                        "accommodation": ["hotel", "resort", "stay", "accommodation", "room"],
                        "destination": ["destination", "place", "location", "city", "country"],
                        "activities": ["activity", "activities", "do", "visit", "see", "experience"],
                        "food": ["food", "meal", "eat", "cuisine", "restaurant", "diet"]
                    }
                    
                    for field, indicators in update_field_indicators.items():
                        if any(indicator in user_input for indicator in indicators):
                            is_update = True

                    # Check if websocket is still connected before database operations
                    if websocket.client_state.name != "CONNECTED":
                        break

                    db[COLLECTION_NAME].update_one(
                        {"session_id": session_id},
                        {"$push": {"messages": user_msg}, "$set": {"updated_at": datetime.now()}}
                    )

                    await manager.broadcast_to_session(session_id, {
                        "type": "message",
                        "data": {"message": user_msg}
                    })
                    await manager.broadcast_to_session(session_id, {"type": "typing", "data": {"status": True}})

                    session = db[COLLECTION_NAME].find_one({"session_id": session_id})
                    if not session:
                        await manager.send_personal_message({"type": "error", "data": {"message": "Session lost"}}, websocket)
                        continue

                    history = session.get("messages", [])
                    combined_data = session.get("latest_analysis", {}).get("extracted_data", {}).copy()

                    update_instruction = ""
                    if is_update:
                        update_instruction = """
                        IMPORTANT: This is an update request. The user wants to CHANGE one or more fields they provided earlier.
                        Carefully identify what fields are being updated and extract the new values.
                        For example, if they say "change my trip to 9 days", extract travel_period as "9 days".
                        Only include fields that are being updated in your JSON response.
                        """
                    
                    messages = [
                        {"role": "system", "content": SYSTEM_PROMPT.format(
                            question_set=json.dumps(QUESTION_SET, indent=2),
                            chat_history=format_chat_history(history)
                        ) + "\nIMPORTANT: Return ONLY the text response without any JSON structure or metadata. Do not include any JSON in your response."},
                        {"role": "user", "content": user_msg["content"]}
                    ]

                    try:
                        stream = openai.ChatCompletion.create(
                            model=GPT_MODEL,  
                            messages=messages,
                            temperature=0.5,
                            stream=True
                        )

                        full_response = ""
                        for chunk in stream:
                            # Check if websocket is still connected before sending chunks
                            if websocket.client_state.name != "CONNECTED":
                                break
                                
                            if chunk.choices[0].delta.get("content"):
                                token = chunk.choices[0].delta.content
                                full_response += token
                                await manager.broadcast_to_session(session_id, {
                                    "type": "stream_chunk",
                                    "data": {"content": token}
                                })
                                await asyncio.sleep(0.01)
                    except Exception as e:
                        print(f"Error in streaming response: {str(e)}")
                        if websocket.client_state.name == "CONNECTED":
                            await manager.broadcast_to_session(session_id, {
                                "type": "error", 
                                "data": {"message": f"Error generating response: {str(e)}"}
                            })
                        continue

                    # Analysis and data extraction
                    try:
                        analysis_messages = [
                            {"role": "system", "content": SYSTEM_PROMPT.format(
                                question_set=json.dumps(QUESTION_SET, indent=2),
                                chat_history=format_chat_history(history)
                            ) + update_instruction},
                            {"role": "user", "content": user_msg["content"]}
                        ]
                        
                        if is_update:
                            analysis_messages.append({
                                "role": "system", 
                                "content": f"""
                                Remember, this is an UPDATE request. The user wants to change something in their existing data.
                                Current data: {json.dumps(combined_data, indent=2)}
                                
                                Extract ONLY the fields being updated. Your response should be valid JSON with ONLY the updated fields.
                                Example format: {{"travel_period": "9 days"}}
                                """
                            })
                        
                        analysis_response = openai.ChatCompletion.create(
                            model=GPT_MODEL,  
                            messages=analysis_messages,
                            temperature=0.5
                        )
                        
                        analysis_text = analysis_response.choices[0].message.content
                        
                        analysis = None
                        try:
                            analysis = json.loads(analysis_text)
                        except json.JSONDecodeError:
                            json_pattern = r'```json\s*([\s\S]*?)\s*```|{\s*"[^"]+"\s*:|{\s*\'[^\']+\'\s*:'
                            json_match = re.search(json_pattern, analysis_text)
                            
                            if json_match:
                                json_str = json_match.group(1) if json_match.group(1) else analysis_text
                                json_str = re.sub(r'```json|```', '', json_str).strip()
                                try:
                                    analysis = json.loads(json_str)
                                except json.JSONDecodeError:
                                    analysis = None
                        
                        if not analysis:
                            analysis = {
                                "response": full_response, 
                                "extracted_data": {}, 
                                "missing_fields": list(QUESTION_SET.keys()), 
                                "complete": False
                            }
                            
                        if "extracted_data" not in analysis and "response" not in analysis:
                            analysis = {
                                "response": full_response,
                                "extracted_data": analysis,
                                "missing_fields": [],
                                "complete": False
                            }
                            
                    except Exception as e:
                        analysis = {
                            "response": full_response, 
                            "extracted_data": {}, 
                            "missing_fields": list(QUESTION_SET.keys()), 
                            "complete": False
                        }
                        print(f"Error analyzing response: {str(e)}")

                    # Process extracted data
                    new_data = {}
                    if "extracted_data" in analysis:
                        new_data = analysis.get("extracted_data", {})
                    else:
                        new_data = analysis
                        
                    if isinstance(new_data, str):
                        try:
                            new_data = json.loads(new_data)
                        except:
                            new_data = {}
                    
                    # Update combined data
                    for key, val in new_data.items():
                        if val is not None and val != "": 
                            if key == "activities":
                                current_activities = combined_data.get(key, [])
                                if not isinstance(current_activities, list):
                                    current_activities = []
                                
                                if isinstance(val, list):
                                    new_activities = val
                                elif isinstance(val, str):
                                    new_activities = [val]  
                                else:
                                    new_activities = []
                                    
                                if is_update and len(new_activities) <= 2:
                                    activity_update_words = ["add", "include", "also"]
                                    is_addition = any(word in user_msg["content"].lower() for word in activity_update_words)
                                    
                                    if is_addition:
                                        combined_data[key] = list(set(current_activities + new_activities))
                                    else:
                                        combined_data[key] = new_activities
                                else:
                                    combined_data[key] = list(set(current_activities + new_activities))
                            else:
                                combined_data[key] = val

                    missing = [k for k in QUESTION_SET.keys() if not combined_data.get(k)]
                    complete = len(missing) == 0

                    assistant_msg = {
                        "role": "assistant",
                        "content": full_response,
                        "timestamp": datetime.now().isoformat()
                    }

                    # Update database
                    try:
                        db[COLLECTION_NAME].update_one(
                            {"session_id": session_id},
                            {
                                "$push": {"messages": assistant_msg},
                                "$set": {
                                    "updated_at": datetime.now(),
                                    "latest_analysis": {
                                        "missing_fields": missing,
                                        "complete": complete,
                                        "extracted_data": combined_data
                                    }
                                }
                            },
                            upsert=True
                        )
                    except Exception as e:
                        print(f"Database update error: {str(e)}")
                        if websocket.client_state.name == "CONNECTED":
                            await manager.broadcast_to_session(session_id, {
                                "type": "error", 
                                "data": {"message": f"Database error: {str(e)}"}
                            })
                        
                    # Send extraction update
                    if websocket.client_state.name == "CONNECTED":
                        await manager.broadcast_to_session(session_id, {
                            "type": "extraction_update",
                            "data": {
                                "extracted_data": combined_data,
                                "missing_fields": missing,
                                "complete": complete
                            }
                        })

                        await manager.broadcast_to_session(session_id, {
                            "type": "message_complete",
                            "data": {"message": assistant_msg}
                        })

                    # Generate itinerary if complete
                    if complete and websocket.client_state.name == "CONNECTED":
                        await manager.broadcast_to_session(session_id, {
                            "type": "itinerary_status",
                            "data": {"status": "generating"}
                        })

                        try:
                            system_msg = ITINERARY_AI_MESSAGE.format(**combined_data)
                            itinerary_stream = openai.ChatCompletion.create(
                                model=GPT_MODEL, 
                                messages=[
                                    {"role": "system", "content": system_msg},
                                    {"role": "user", "content": "Please generate the itinerary based on given data. Generate itinerary without repetition."}
                                ],
                                temperature=0.3,
                                stream=True
                            )

                            full_itinerary = ""
                            for chunk in itinerary_stream:
                                if websocket.client_state.name != "CONNECTED":
                                    break
                                    
                                if chunk.choices[0].delta.get("content"):
                                    token = chunk.choices[0].delta.content
                                    full_itinerary += token
                                    await manager.broadcast_to_session(session_id, {
                                        "type": "itinerary_chunk",
                                        "data": {"content": token}
                                    })
                                    await asyncio.sleep(0.01)
                                    
                            # Store the itinerary
                            if websocket.client_state.name == "CONNECTED":
                                try:
                                    db[COLLECTION_NAME].update_one(
                                        {"session_id": session_id},
                                        {
                                            "$set": {
                                                "itinerary": full_itinerary,
                                                "itinerary_generated_at": datetime.now().isoformat()
                                            }
                                        }
                                    )
                                except Exception as db_err:
                                    print(f"Failed to save itinerary: {db_err}")
                                    
                                await manager.broadcast_to_session(session_id, {
                                    "type": "itinerary_complete",
                                    "data": {"itinerary": full_itinerary}
                                })
                        except Exception as e:
                            print(f"Error generating itinerary: {str(e)}")
                            if websocket.client_state.name == "CONNECTED":
                                await manager.broadcast_to_session(session_id, {
                                    "type": "error", 
                                    "data": {"message": f"Itinerary generation error: {str(e)}"}
                                })

                except Exception as e:
                    print(f"Error processing message: {str(e)}")
                    if websocket.client_state.name == "CONNECTED":
                        await manager.broadcast_to_session(session_id, {
                            "type": "error", 
                            "data": {"message": f"Error processing message: {str(e)}"}
                        })

            await asyncio.sleep(0.01)

    except WebSocketDisconnect:
        print(f"WebSocket disconnected for session {session_id}")
    except ConnectionAbortedError:
        print(f"Connection aborted for session {session_id}")
    except ConnectionResetError:
        print(f"Connection reset for session {session_id}")
    except Exception as e:
        print(f"WebSocket error: {e}")
        try:
            if websocket.client_state.name == "CONNECTED":
                await manager.broadcast_to_session(session_id, {
                    "type": "error",
                    "data": {"message": f"Connection error: {str(e)}"}
                })
        except:
            pass
    finally:
        # Ensure proper cleanup
        manager.disconnect(websocket, session_id)




@chat_router.get('/check-itinerary-status')
def final_itinerary_status(session_id:str):
    is_complete = is_user_input_complete(session_id)
    if is_complete == True:
        return {"status_code":"200","status":is_complete,"message":"final_itinerary_message"}
    return {"status_code":404,"status":is_complete,"message":"not found"}
    