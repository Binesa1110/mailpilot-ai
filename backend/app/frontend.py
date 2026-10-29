from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles


class SpaStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        _, stat_result = self.lookup_path(path)
        if stat_result is None:
            request_path = scope.get("path", "")
            if request_path == "/api" or request_path.startswith("/api/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            return await super().get_response("index.html", scope)

        try:
            response = await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404:
                raise
            return await super().get_response("index.html", scope)
        return response


def frontend_build_directory() -> Path:
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"
