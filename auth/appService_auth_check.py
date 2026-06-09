from fastapi import Request, HTTPException

async def appService_auth_check(request: Request):
    
    session_cookie = request.cookies.get("AppServiceAuthSession")
    
    user_id = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")

    if not session_cookie and not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    return