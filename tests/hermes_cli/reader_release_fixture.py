"""Real HTTP/uvicorn + SQLite FD + MCP stdio lifecycle, disposable processes only."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sqlite3
import sys


if __name__ == "__main__" and "--mcp" in sys.argv:
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    root = Path(os.environ["HERMES_HOME"])
    async def mcp_main():
        server = Server("reader-release-disposable")
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    # This helper owns an actual source descriptor; cwd alone is not a reader.
    with open(root / "source" / "reader.txt"):
        (root / "mcp.pid").write_text(str(os.getpid()))
        asyncio.run(mcp_main())
    (root / "mcp.closed").write_text("closed")
    raise SystemExit(0)


def run():
    import uvicorn
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from hermes_cli import web_server, web_server_idle_proof
    from hermes_cli.backend_retirement import retirement
    from hermes_cli.reader_release import ReaderExit
    from gateway import host_rendezvous as hr

    root = Path(os.environ["HERMES_HOME"])
    # Refuse standalone use unless the host-wide record AND token are disposable.
    # HERMES_HOME alone does not scope gateway.host_rendezvous.
    lock_dir = Path(os.environ["HERMES_GATEWAY_LOCK_DIR"]).resolve()
    if lock_dir != (root / "gateway-locks").resolve():
        raise RuntimeError("fixture requires disposable gateway rendezvous")
    if hr.host_state_dir().resolve() != lock_dir:
        raise RuntimeError("gateway rendezvous isolation was not honored")
    # No model calls, real profiles, cleanup reapers, cron, gateway startup or
    # production resources. The actual native router/auth/fence/operator run.
    web_server._SESSION_TOKEN = "disposable-only-not-a-production-credential"
    web_server_idle_proof.idle_proof = lambda: {"idle": not (root / "busy").exists()}
    @asynccontextmanager
    async def lifespan(app):
        db = sqlite3.connect(root / "source" / "state.db")
        db.execute("create table if not exists fixture(value)")
        handle = open(root / "source" / "reader.txt")
        params = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve()), "--mcp"], env=dict(os.environ))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield
        while (root / "hold-shutdown").exists():
            await asyncio.sleep(.02)
        handle.close()
        db.close()
        (root / "fds.closed").write_text("closed")
    web_server.app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(web_server.app, host="127.0.0.1", port=0, log_level="error"))
    async def serve():
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("startup failed")
            await asyncio.sleep(.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        owner = ReaderExit(server, port, hr.ROLE_DESKTOP_SERVE)
        web_server.app.state.reader_exit = owner
        hr.claim_host_lock(hr.ROLE_DESKTOP_SERVE)
        hr.publish_record(hr.ROLE_DESKTOP_SERVE, host="127.0.0.1", port=port,
                          profiles=[], token=web_server._SESSION_TOKEN)
        manifest = {"expected": owner.identity, "sources": [str(root / "source")],
                    "exclude_pids": [os.getppid(), 2552538]}
        (root / "manifest.json").write_text(json.dumps(manifest))
        await task
        assert retirement.is_committed()
    asyncio.run(serve())


if __name__ == "__main__":
    run()
