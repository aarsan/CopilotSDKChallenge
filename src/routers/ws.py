"""WebSocket routers — governance-chat, concierge-chat, main chat."""

import asyncio
import logging
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from src.copilot_helpers import approve_all

from src.auth import UserContext, get_user_context
from src.config import get_active_model
from src.database import save_chat_message, log_usage
from src.web_shared import ensure_copilot_client, active_sessions

logger = logging.getLogger("infraforge.web")

router = APIRouter()

# ── Module-level session stores ──────────────────────────────
governance_sessions: dict = {}
concierge_sessions: dict = {}


# ── WebSocket: Governance Chat ───────────────────────────────

@router.websocket("/ws/governance-chat")
async def websocket_governance_chat(websocket: WebSocket):
    """WebSocket endpoint for the Governance Advisor agent.

    Specialised chat for discussing policies, security standards, compliance
    frameworks, and submitting policy modification requests. Uses a focused
    agent with governance-only tools.
    """
    from src.agents import GOVERNANCE_AGENT
    from src.tools import get_governance_tools

    await websocket.accept()

    session_token: Optional[str] = None
    user_context: Optional[UserContext] = None

    # ── Serialised send infrastructure ───────────────────────
    send_queue: asyncio.Queue = asyncio.Queue()
    ws_closed = False
    loop = asyncio.get_running_loop()

    async def _ws_sender():
        nonlocal ws_closed
        while True:
            msg = await send_queue.get()
            if msg is None:
                break
            if ws_closed:
                continue
            try:
                await websocket.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
            except Exception:
                ws_closed = True

    sender_task = asyncio.create_task(_ws_sender())

    def _enqueue(data: dict):
        if ws_closed:
            return
        loop.call_soon_threadsafe(send_queue.put_nowait, data)

    try:
        # ── Step 1: Authenticate ─────────────────────────────
        auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)

        if auth_msg.get("type") != "auth":
            await websocket.send_json({"type": "error", "message": "Expected auth message"})
            await websocket.close()
            return

        session_token = auth_msg.get("sessionToken", "")
        user_context = await get_user_context(session_token)

        if not user_context:
            await websocket.send_json({"type": "error", "message": "Invalid or expired session"})
            await websocket.close()
            return

        # ── Step 2: Create Copilot session with governance context ─
        client = await ensure_copilot_client()
        if client is None:
            await websocket.send_json({
                "type": "error",
                "message": "Copilot SDK is not available. Governance chat is disabled.",
            })
            await websocket.close()
            return

        personalized_system_message = (
            GOVERNANCE_AGENT.system_prompt + "\n" + user_context.to_prompt_context()
        )

        tools = get_governance_tools()
        try:
            copilot_session = await client.create_session({
                "model": get_active_model(),
                "streaming": True,
                "tools": tools,
                "system_message": {"content": personalized_system_message},
                "on_permission_request": approve_all,
            })
        except Exception as e:
            logger.error(f"Failed to create Governance session: {e}")
            await websocket.send_json({
                "type": "error",
                "message": f"Failed to create governance chat session: {e}",
            })
            await websocket.close()
            return

        gov_key = f"gov-{session_token}"
        governance_sessions[gov_key] = {
            "copilot_session": copilot_session,
            "user_context": user_context,
            "connected_at": time.time(),
        }
        await websocket.send_json({
            "type": "auth_ok",
            "user": {
                "displayName": user_context.display_name,
                "email": user_context.email,
                "department": user_context.department,
                "team": user_context.team,
            },
        })

        # ── Step 3: Chat loop ────────────────────────────────
        while True:
            data = await websocket.receive_json()

            if data.get("type") == "message":
                user_message = data.get("content", "").strip()
                if not user_message:
                    continue

                # Stream the response
                response_chunks: list[str] = []
                done_event = asyncio.Event()

                def on_event(event):
                    try:
                        evt_type = event.type.value
                        if evt_type == "assistant.message_delta":
                            delta = event.data.delta_content or ""
                            response_chunks.append(delta)
                            _enqueue({"type": "delta", "content": delta})
                        elif evt_type == "assistant.message":
                            full_content = event.data.content or ""
                            if full_content:
                                _enqueue({"type": "done", "content": full_content})
                        elif evt_type in ("tool.call", "tool.execution_start"):
                            tool_name = getattr(event.data, 'name', 'unknown')
                            _enqueue({"type": "tool_call", "name": tool_name, "status": "running"})
                        elif evt_type in ("tool.result", "tool.execution_complete"):
                            tool_name = getattr(event.data, 'name', 'unknown')
                            _enqueue({"type": "tool_call", "name": tool_name, "status": "complete"})
                        elif evt_type == "session.idle":
                            done_event.set()
                    except Exception as e:
                        logger.error(f"Governance event handler error: {e}")
                        done_event.set()

                unsubscribe = copilot_session.on(on_event)

                _ws_msg_start = time.time()
                try:
                    await copilot_session.send({"prompt": user_message})
                    await asyncio.wait_for(done_event.wait(), timeout=120)
                except asyncio.TimeoutError:
                    _enqueue({"type": "error", "message": "Request timed out. Please try again."})
                finally:
                    unsubscribe()

                await asyncio.sleep(0.05)

                # Save conversation
                full_response = "".join(response_chunks)
                await save_chat_message(session_token, "user", f"[governance] {user_message}")
                await save_chat_message(session_token, "assistant", f"[governance] {full_response}")

                # Track agent activity
                from src.copilot_helpers import _record_activity
                _record_activity(
                    agent_name="CISO_ADVISOR",
                    model=get_active_model(),
                    status="ok",
                    duration_ms=(time.time() - _ws_msg_start) * 1000,
                    prompt_len=len(user_message),
                    response_len=len(full_response),
                )

            elif data.get("type") == "ping":
                _enqueue({"type": "pong"})

    except WebSocketDisconnect:
        ws_closed = True
        logger.info(f"Governance chat disconnected: {user_context.email if user_context else 'unknown'}")
    except Exception as e:
        ws_closed = True
        logger.error(f"Governance WebSocket error: {e}")
    finally:
        ws_closed = True
        send_queue.put_nowait(None)
        sender_task.cancel()
        try:
            await sender_task
        except (asyncio.CancelledError, Exception):
            pass


# ── WebSocket: Concierge / CISO Chat ────────────────────────

@router.websocket("/ws/concierge-chat")
async def websocket_concierge_chat(websocket: WebSocket):
    """WebSocket endpoint for the Concierge / CISO Advisor agent.

    An always-available general assistant with CISO-level authority to review,
    modify, and grant exceptions to governance policies. Uses the Concierge
    agent persona with the full CISO tool set.

    Protocol identical to /ws/chat and /ws/governance-chat.
    """
    from src.agents import CONCIERGE_AGENT
    from src.tools import get_concierge_tools

    await websocket.accept()

    session_token: Optional[str] = None
    user_context: Optional[UserContext] = None

    # ── Serialised send infrastructure ───────────────────────
    # SDK event callbacks fire from the event-loop thread but can
    # burst many events concurrently.  Starlette's WebSocket.send
    # is NOT safe for concurrent calls — interleaved frames corrupt
    # the connection.  We funnel every outgoing message through an
    # asyncio.Queue consumed by a single sender task.
    send_queue: asyncio.Queue = asyncio.Queue()
    ws_closed = False
    loop = asyncio.get_running_loop()

    async def _ws_sender():
        """Single consumer: pulls from *send_queue* and writes to the WS."""
        nonlocal ws_closed
        while True:
            msg = await send_queue.get()
            if msg is None:          # poison pill → shut down
                break
            if ws_closed:
                continue
            try:
                await websocket.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
            except Exception:
                ws_closed = True

    sender_task = asyncio.create_task(_ws_sender())

    def _enqueue(data: dict):
        """Non-async helper safe to call from sync on_event callbacks."""
        if ws_closed:
            return
        loop.call_soon_threadsafe(send_queue.put_nowait, data)

    try:
        # ── Step 1: Authenticate ─────────────────────────────
        auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)

        if auth_msg.get("type") != "auth":
            await websocket.send_json({"type": "error", "message": "Expected auth message"})
            await websocket.close()
            return

        session_token = auth_msg.get("sessionToken", "")
        user_context = await get_user_context(session_token)

        if not user_context:
            await websocket.send_json({"type": "error", "message": "Invalid or expired session"})
            await websocket.close()
            return

        # ── Step 2: Create Copilot session with concierge context ─
        client = await ensure_copilot_client()
        if client is None:
            await websocket.send_json({
                "type": "error",
                "message": "Copilot SDK is not available. Concierge is disabled.",
            })
            await websocket.close()
            return

        personalized_system_message = (
            CONCIERGE_AGENT.system_prompt + "\n" + user_context.to_prompt_context()
        )

        tools = get_concierge_tools()
        try:
            copilot_session = await client.create_session({
                "model": get_active_model(),
                "streaming": True,
                "tools": tools,
                "system_message": {"content": personalized_system_message},
                "on_permission_request": approve_all,
            })
        except Exception as e:
            logger.error(f"Failed to create Concierge session: {e}")
            await websocket.send_json({
                "type": "error",
                "message": f"Failed to create concierge session: {e}",
            })
            await websocket.close()
            return

        con_key = f"concierge-{session_token}"
        concierge_sessions[con_key] = {
            "copilot_session": copilot_session,
            "user_context": user_context,
            "connected_at": time.time(),
        }
        await websocket.send_json({
            "type": "auth_ok",
            "user": {
                "displayName": user_context.display_name,
                "email": user_context.email,
                "department": user_context.department,
                "team": user_context.team,
            },
        })

        # ── Step 3: Chat loop ────────────────────────────────
        while True:
            data = await websocket.receive_json()

            if data.get("type") == "message":
                user_message = data.get("content", "").strip()
                if not user_message:
                    continue

                # Stream the response
                response_chunks: list[str] = []
                done_event = asyncio.Event()

                def on_event(event):
                    try:
                        evt_type = event.type.value
                        if evt_type == "assistant.message_delta":
                            delta = event.data.delta_content or ""
                            response_chunks.append(delta)
                            _enqueue({"type": "delta", "content": delta})
                        elif evt_type == "assistant.message":
                            full_content = event.data.content or ""
                            if full_content:
                                _enqueue({"type": "done", "content": full_content})
                        elif evt_type in ("tool.call", "tool.execution_start"):
                            tool_name = getattr(event.data, 'name', 'unknown')
                            _enqueue({"type": "tool_call", "name": tool_name, "status": "running"})
                        elif evt_type in ("tool.result", "tool.execution_complete"):
                            tool_name = getattr(event.data, 'name', 'unknown')
                            _enqueue({"type": "tool_call", "name": tool_name, "status": "complete"})
                        elif evt_type == "session.idle":
                            done_event.set()
                    except Exception as e:
                        logger.error(f"Concierge event handler error: {e}")
                        done_event.set()

                unsubscribe = copilot_session.on(on_event)

                _ws_msg_start = time.time()
                try:
                    await copilot_session.send({"prompt": user_message})
                    await asyncio.wait_for(done_event.wait(), timeout=120)
                except asyncio.TimeoutError:
                    _enqueue({"type": "error", "message": "Request timed out. Please try again."})
                except Exception as send_err:
                    logger.error(f"[Concierge] Error during send: {send_err}", exc_info=True)
                    _enqueue({"type": "error", "message": f"Error: {send_err}"})
                    done_event.set()
                finally:
                    unsubscribe()

                # Give the sender task a moment to flush queued messages
                await asyncio.sleep(0.05)

                # Save conversation
                full_response = "".join(response_chunks)
                await save_chat_message(session_token, "user", f"[concierge] {user_message}")
                await save_chat_message(session_token, "assistant", f"[concierge] {full_response}")

                # Track agent activity
                from src.copilot_helpers import _record_activity
                _record_activity(
                    agent_name="CONCIERGE",
                    model=get_active_model(),
                    status="ok",
                    duration_ms=(time.time() - _ws_msg_start) * 1000,
                    prompt_len=len(user_message),
                    response_len=len(full_response),
                )

            elif data.get("type") == "ping":
                _enqueue({"type": "pong"})

    except WebSocketDisconnect:
        ws_closed = True
        logger.info(f"Concierge disconnected: {user_context.email if user_context else 'unknown'}")
    except Exception as e:
        ws_closed = True
        logger.error(f"Concierge WebSocket error: {e}")
    finally:
        ws_closed = True
        send_queue.put_nowait(None)   # poison pill
        sender_task.cancel()
        try:
            await sender_task
        except (asyncio.CancelledError, Exception):
            pass


# ── WebSocket Chat ───────────────────────────────────────────

@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket endpoint for streaming chat with InfraForge.

    Protocol:
    1. Client connects and sends: {"type": "auth", "sessionToken": "..."}
    2. Server validates and responds: {"type": "auth_ok", "user": {...}}
    3. Client sends messages: {"type": "message", "content": "..."}
    4. Server streams responses: {"type": "delta", "content": "..."} chunks
    5. Server sends completion: {"type": "done", "content": "full response"}
    6. Server sends tool calls: {"type": "tool_call", "name": "...", "status": "..."}
    """
    from src.agents import WEB_CHAT_AGENT
    from src.tools import get_all_tools

    await websocket.accept()

    session_token: Optional[str] = None
    user_context: Optional[UserContext] = None

    # ── Serialised send infrastructure ───────────────────────
    send_queue: asyncio.Queue = asyncio.Queue()
    ws_closed = False
    loop = asyncio.get_running_loop()

    async def _ws_sender():
        nonlocal ws_closed
        while True:
            msg = await send_queue.get()
            if msg is None:
                break
            if ws_closed:
                continue
            try:
                await websocket.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
            except Exception:
                ws_closed = True

    sender_task = asyncio.create_task(_ws_sender())

    def _enqueue(data: dict):
        if ws_closed:
            return
        loop.call_soon_threadsafe(send_queue.put_nowait, data)

    try:
        # ── Step 1: Authenticate ─────────────────────────────
        auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)

        if auth_msg.get("type") != "auth":
            await websocket.send_json({"type": "error", "message": "Expected auth message"})
            await websocket.close()
            return

        session_token = auth_msg.get("sessionToken", "")
        user_context = await get_user_context(session_token)

        if not user_context:
            await websocket.send_json({"type": "error", "message": "Invalid or expired session"})
            await websocket.close()
            return

        # ── Step 2: Create Copilot session with user context ─
        client = await ensure_copilot_client()
        if client is None:
            await websocket.send_json({
                "type": "error",
                "message": "Copilot SDK is not available. Chat is disabled but the rest of InfraForge works.",
            })
            await websocket.close()
            return

        personalized_system_message = (
            WEB_CHAT_AGENT.system_prompt + "\n" + user_context.to_prompt_context()
        )

        tools = get_all_tools()
        try:
            copilot_session = await client.create_session({
                "model": get_active_model(),
                "streaming": True,
                "tools": tools,
                "system_message": {"content": personalized_system_message},
                "on_permission_request": approve_all,
            })
        except Exception as e:
            logger.error(f"Failed to create Copilot session: {e}")
            await websocket.send_json({
                "type": "error",
                "message": f"Failed to create chat session: {e}",
            })
            await websocket.close()
            return

        active_sessions[session_token] = {
            "copilot_session": copilot_session,
            "user_context": user_context,
            "connected_at": time.time(),
        }
        await websocket.send_json({
            "type": "auth_ok",
            "user": {
                "displayName": user_context.display_name,
                "email": user_context.email,
                "department": user_context.department,
                "team": user_context.team,
            },
        })

        # ── Step 3: Chat loop ────────────────────────────────
        while True:
            data = await websocket.receive_json()

            if data.get("type") == "message":
                user_message = data.get("content", "").strip()
                if not user_message:
                    continue

                # Track for analytics
                request_record = {
                    "timestamp": time.time(),
                    "user": user_context.email,
                    "department": user_context.department,
                    "cost_center": user_context.cost_center,
                    "prompt": user_message[:200],  # Truncate for privacy
                    "resource_types": [],
                    "estimated_cost": 0.0,
                    "from_catalog": False,
                }

                # Stream the response
                response_chunks: list[str] = []
                done_event = asyncio.Event()

                def on_event(event):
                    try:
                        evt_type = event.type.value
                        if evt_type == "assistant.message_delta":
                            delta = event.data.delta_content or ""
                            response_chunks.append(delta)
                            _enqueue({"type": "delta", "content": delta})
                        elif evt_type == "assistant.message":
                            full_content = event.data.content or ""
                            if full_content:
                                _enqueue({"type": "done", "content": full_content})
                        elif evt_type in ("tool.call", "tool.execution_start"):
                            tool_name = getattr(event.data, 'name', 'unknown')
                            print(f"[TOOL] {evt_type}: {tool_name}")
                            _enqueue({"type": "tool_call", "name": tool_name, "status": "running"})
                            if tool_name == "search_template_catalog":
                                request_record["from_catalog"] = True
                        elif evt_type in ("tool.result", "tool.execution_complete"):
                            tool_name = getattr(event.data, 'name', 'unknown')
                            print(f"[TOOL] {evt_type}: {tool_name}")
                            _enqueue({"type": "tool_call", "name": tool_name, "status": "complete"})
                        elif evt_type == "session.idle":
                            done_event.set()
                    except Exception as e:
                        logger.error(f"Event handler error: {e}")
                        done_event.set()

                unsubscribe = copilot_session.on(on_event)

                _ws_msg_start = time.time()
                try:
                    await copilot_session.send({"prompt": user_message})
                    await asyncio.wait_for(done_event.wait(), timeout=120)
                except asyncio.TimeoutError:
                    _enqueue({"type": "error", "message": "Request timed out. Please try again."})
                finally:
                    unsubscribe()

                await asyncio.sleep(0.05)

                # Persist to database
                full_response = "".join(response_chunks)
                await save_chat_message(session_token, "user", user_message)
                await save_chat_message(session_token, "assistant", full_response)
                await log_usage(request_record)

                # Track agent activity
                from src.copilot_helpers import _record_activity
                _record_activity(
                    agent_name="WEB_CHAT",
                    model=get_active_model(),
                    status="ok",
                    duration_ms=(time.time() - _ws_msg_start) * 1000,
                    prompt_len=len(user_message),
                    response_len=len(full_response),
                )

            elif data.get("type") == "ping":
                _enqueue({"type": "pong"})

    except WebSocketDisconnect:
        ws_closed = True
        logger.info(f"Client disconnected: {user_context.email if user_context else 'unknown'}")
    except Exception as e:
        ws_closed = True
        logger.error(f"WebSocket error: {e}")
    finally:
        ws_closed = True
        send_queue.put_nowait(None)
        sender_task.cancel()
        try:
            await sender_task
        except (asyncio.CancelledError, Exception):
            pass
