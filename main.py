"""本地启动入口：uvicorn main:app --reload 或 uv run python main.py"""

from app.api import app

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
