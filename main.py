import uvicorn

from app.api import create_app

app = create_app()


@app.get("/")
def read_root():
    return {"service": "device-fleet-ota"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
