"""Reserved blank site for the next QuantInSight project."""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app=FastAPI(title='QuantInSight',docs_url=None,redoc_url=None,openapi_url=None)

@app.get('/',response_class=HTMLResponse)
def index():
    return HTMLResponse('<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>QuantInSight</title></head><body></body></html>',headers={'Cache-Control':'no-store'})
