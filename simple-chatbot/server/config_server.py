import asyncio
import json
import os
from typing import Optional

from aiohttp import web
from loguru import logger


CONFIG_SERVER_PORT = int(os.getenv("CONFIG_SERVER_PORT","7861"))


def _get_config_file_path() -> str:
    script_dir = os.path.dirname(__file__)
    return os.path.join(script_dir, "interview_config.json")


def save_interview_config(bot_nature: str, jd: str) -> bool:
    config_file = _get_config_file_path()
    if bot_nature not in ["friendly", "decent", "strict"]:
        logger.warning(f"Invalid botNature '{bot_nature}', defaulting to 'decent'")
        bot_nature = "decent"
    config = {"botNature": bot_nature, "jd": jd}
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info("saved config file")
        return True
    except Exception as e:
        logger.error(f"error saving config file: {e}")
        return False

async def handle_interview_config(request: web.Request) -> web.Response:
    """
    Implementation will be done
    """

    try:
        data = await request.json()

        bot_nature= data.get("botNature","decent")
        jd = data.get("jd", "")

        valid_natures = ["friendly","decent","strict"]
        if bot_nature not in valid_natures:
            return web.json_response(
                {
                    "error": f"Invalid botNature. Must be one of: {', '.join(valid_natures)}"
                },
                status=400,
            )
        
        if not jd or not jd.strip():
            return web.json_response(
                {"error": "Job description (jd) is required and cannot be empty"},
                status=400,
            )
        
        success = save_interview_config(bot_nature,jd.strip())

        if success:
            logger.info(f"Config saved successfully - Nature: {bot_nature}, JD length: {len(jd)}")
            return web.json_response(
                {
                    "success": True,
                    "message": "Configuration saved successfully",
                    "botNature": bot_nature,
                    "jdLength": len(jd),
                }
            )
        else:
            return web.json_response(
                {"error":"failed to save the config"}
            )
    except json.JSONDecodeError:
        return web.json_response(
            {"error":"Invalid JSON in the request body"}
        )
    except Exception as e:
        logger.error(f"Error handling config request: {e}")
        return web.json_response(
            {"error": f"Internal server error: {str(e)}"},
            status=500,
        )




async def handle_health_check(request: web.Request) -> web.Response:
    """
    Implementation will be done
    """
    return web.json_response({"status":"Ok", "service":"config-server"})


def create_app() -> web.Application:
    """
    Implementation will be done
    """
    app = web.Application()

    async def cors_middleware(app, handler):
        async def middleware_handler(request):
            if request.method == "OPTIONS":
                response = web.Response()
            else:
                response = await handler(request)

            allowed_origins_str = os.getenv("ALLOWED_ORIGINS", "*")
            if allowed_origins_str == "*":
                response.headers["Access-Control-Allow-Origin"] = "*"

            else:
                allowed_origins = [origin.strip() for origin in allowed_origins_str.split(",")]
                origin = request.headers.get("Origin", "")
                if origin in allowed_origins:
                    response.headers["Access-Control-Allow-Origin"] = origin
                else:
                    response.headers["Access-Control-Allow-Origin"] = allowed_origins[0] if allowed_origins else "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            return response
        return middleware_handler
    app.middlewares.append(cors_middleware)
    app.router.add_post("/api/interview-config", handle_interview_config)
    app.router.add_get("/health", handle_health_check)
    return app



async def run_config_server(port: Optional[int]=None)-> None:
    '''
    Implementation will be done
    '''
    if port is None:
        port = CONFIG_SERVER_PORT

    app = create_app()
    runner = web.AppRunner(app) #AppRunner handles all async operations for the application
    await runner.setup()

    host = os.getenv("CONFIG_SERVER_HOST", "0.0.0.0")
    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info(f"Config server running on http://{host}:{port}")
    logger.info(f"Endpoint: POST http://{host}:{port}/api/interview-config")

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt: 
        logger.info("Shutting down config server...")
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run_config_server())
