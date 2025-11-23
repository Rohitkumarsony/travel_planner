from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI
from app.router import chat_router
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Create FastAPI app
app = FastAPI(title="Advanced Chatbot API")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static directory
app.mount("/static", StaticFiles(directory="static"), name="static")
# Also serve static under prefixed path to support relative links from /Travelliko
app.mount("/Travelliko/static", StaticFiles(directory="static"), name="static_prefixed")

# Landing pages
@app.get("/", include_in_schema=False)
async def travel_planner_index():
    return FileResponse("static/landing-page.html")


@app.get("/Travelliko/travel-plan", include_in_schema=False)
async def travel_planner_plan():
    return FileResponse("static/travel-plan.html")


@app.get("/static/image", include_in_schema=False)
async def image_demo_bg():
    return FileResponse("static/image/demo-bg.jpg")


@app.get("/static/images", include_in_schema=False)
async def image_travel_bg():
    return FileResponse("static/image/travel-bg.jpg")

# Aliases for clients requesting under /Travelliko prefix
@app.get("/Travelliko/static/image", include_in_schema=False)
async def image_demo_bg_prefixed():
    return FileResponse("static/image/demo-bg.jpg")


@app.get("/Travelliko/static/images", include_in_schema=False)
async def image_travel_bg_prefixed():
    return FileResponse("static/image/travel-bg.jpg")


# API Router
app.include_router(chat_router, prefix="/Travelliko")