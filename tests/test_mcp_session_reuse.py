import pytest
import httpx

from mcp_studio.mcp_client import MCPClient


@pytest.mark.asyncio
async def test_close_clears_session_id():
    client = MCPClient('http://example.invalid/mcp')
    client.session_id = 'dead-session'

    async def handler(request: httpx.Request):
        assert request.method == 'DELETE'
        assert request.headers.get('mcp-session-id') == 'dead-session'
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        await client._close(http)

    assert client.session_id is None
