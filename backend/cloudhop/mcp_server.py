"""Cloudhop as an MCP server (Streamable HTTP, plain JSON answers, no server-sent events).

AI assistants connect with a personal MCP token and get tools that read a user's projects and propose changes.
No tool approves or runs anything: proposals come back with a link to the dashboard, where a person approves.

ponytail: stateless, one JSON answer per request, no SSE stream and no session id. Enough for tools that answer in
seconds; add the event stream when a tool needs to report progress.
"""
import json
import logging

from .errors import RepoError

log = logging.getLogger('cloudhop.mcp')

PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
INSTRUCTIONS = ('Cloudhop deploys apps to the user\'s clouds and hosting platforms. These tools read projects, status and '
                'activity, and propose actions or plan moves. Nothing runs from here: every change waits for the user to '
                'approve it in the Cloudhop dashboard, so give them the link each proposal returns.')


class Tool:
    def __init__(self, name, description, properties, required, run, read_only=True):
        self.name, self.run, self.read_only = name, run, read_only
        self.spec = {
            'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False},
            # Planning writes a dry-run record, so it is not read-only, but it never changes a cloud or platform.
            'annotations': {'readOnlyHint': read_only, 'destructiveHint': False, 'openWorldHint': False},
        }

    def check(self, arguments):
        """Arguments must match the schema's names and be strings, so model output cannot reach code as other types."""
        schema = self.spec['inputSchema']
        if not isinstance(arguments, dict) or not set(schema['required']) <= set(arguments) <= set(schema['properties']):
            return False
        return all(isinstance(value, str) and len(value) <= 2000 for value in arguments.values())


def _result(message_id, result):
    return {'jsonrpc': '2.0', 'id': message_id, 'result': result}


def _error(message_id, code, message):
    return {'jsonrpc': '2.0', 'id': message_id, 'error': {'code': code, 'message': message}}


async def handle(message, tools, user):
    """One JSON-RPC message to its answer, or None for a notification. `tools` maps name to Tool."""
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
        return _error(message.get('id') if isinstance(message, dict) else None, -32600, 'Invalid request')
    method, message_id, params = message['method'], message.get('id'), message.get('params') or {}
    if 'id' not in message:
        return None  # notifications/initialized and friends need no answer
    if method == 'initialize':
        asked = params.get('protocolVersion') if isinstance(params, dict) else None
        return _result(message_id, {
            'protocolVersion': asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'cloudhop', 'title': 'Cloudhop', 'version': '1.0.0'},
            'instructions': INSTRUCTIONS,
        })
    if method == 'ping':
        return _result(message_id, {})
    if method == 'tools/list':
        return _result(message_id, {'tools': [tool.spec for tool in tools.values()]})
    if method == 'tools/call':
        tool = tools.get(params.get('name')) if isinstance(params, dict) else None
        if not tool:
            return _error(message_id, -32602, 'Unknown tool')
        arguments = params.get('arguments') or {}
        if not tool.check(arguments):
            return _error(message_id, -32602, f'Arguments for {tool.name} do not match its input schema')
        try:
            output = await tool.run(user, **arguments)
        except RepoError as error:
            # Tool errors go back to the model as results it can read and act on, not protocol errors.
            return _result(message_id, {'content': [{'type': 'text', 'text': error.message}], 'isError': True})
        except Exception:  # noqa: BLE001 - one broken tool call must not take the endpoint down
            log.exception('MCP tool %s failed', tool.name)
            return _result(message_id, {'content': [{'type': 'text', 'text': 'Something went wrong on our side. Try again.'}], 'isError': True})
        return _result(message_id, {'content': [{'type': 'text', 'text': json.dumps(output, indent=1)}], 'structuredContent': output, 'isError': False})
    return _error(message_id, -32601, f'Method not found: {method}')
