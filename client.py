"""
Battleship Game Client
CS 457 - Computer Networks
Author: Garret Gasper

This module handles client-side networking and game logic for a multiplayer
Battleship game played over TCP with newline-delimited JSON payloads.

Architecture & Concurrency:
- Finite State Machine (FSM): INIT -> WAITING_FOR_PLAYERS -> GAME_START ->
  PLAYER_TURN / WAITING_FOR_TURN -> EVALUATE_MOVE -> GAME_OVER -> CLEANUP.
- Concurrency: Background listener thread receives and frames streaming JSON
  messages from the server, while the main thread manages user input and turn execution.
"""

import argparse
import json
import re
import signal
import socket
import sys
import threading
import time
from enum import Enum
from typing import Optional, Tuple


class ClientState(str, Enum):
    """Client finite state machine states."""
    INIT = "INIT"
    WAITING_FOR_PLAYERS = "WAITING_FOR_PLAYERS"
    GAME_START = "GAME_START"
    PLAYER_TURN = "PLAYER_TURN"
    WAITING_FOR_TURN = "WAITING_FOR_TURN"
    EVALUATE_MOVE = "EVALUATE_MOVE"
    GAME_OVER = "GAME_OVER"
    CONNECTION_INTERRUPTED = "CONNECTION_INTERRUPTED"
    CLEANUP = "CLEANUP"
    DISCONNECTED = "DISCONNECTED"


def parse_arguments() -> argparse.Namespace:
    """
    Parses command-line arguments using argparse.
    Accepts:
      -h: Displays help instructions
      -i: Server DNS hostname (required; resolved via socket.gethostbyname())
      -p: Server port (required)
      -u: Player ID / username (optional; defaults to 'Player')
    """
    parser = argparse.ArgumentParser(
        prog="client.py",
        description="Multiplayer Battleship Network Game Client (CS 457)",
        add_help=True
    )
    parser.add_argument(
        "-i", "--ip", "--hostname",
        dest="hostname",
        required=True,
        type=str,
        metavar="HOSTNAME",
        help="Server DNS hostname (required; resolved via socket.gethostbyname())"
    )
    parser.add_argument(
        "-p", "--port",
        dest="port",
        required=True,
        type=int,
        metavar="PORT",
        help="Server port (required)"
    )
    parser.add_argument(
        "-u", "--player", "--player-id",
        dest="player_id",
        default="Player",
        type=str,
        metavar="PLAYER_ID",
        help="Player identifier / username (optional; default: 'Player')"
    )
    return parser.parse_args()


def resolve_hostname(hostname: str) -> str:
    """
    Resolves the server DNS hostname to an IPv4 address using socket.gethostbyname().
    Catches DNS resolution errors explicitly and exits gracefully with a user-friendly
    error message instructing the user to check their router DNS configuration.
    """
    try:
        resolved_ip = socket.gethostbyname(hostname)
        return resolved_ip
    except socket.gaierror:
        print(f"Error: Unable to resolve hostname '{hostname}'. "
              f"Please check your router DNS configuration and network settings.")
        sys.exit(1)
    except socket.herror:
        print(f"Error: Host lookup error for '{hostname}'. "
              f"Please check your router DNS configuration.")
        sys.exit(1)
    except OSError as e:
        print(f"Error: Network error encountered while resolving '{hostname}': {e}. "
              f"Please check your router DNS configuration.")
        sys.exit(1)
    except Exception as e:
        print(f"Error: Unexpected error resolving hostname '{hostname}': {e}. "
              f"Please check your router DNS configuration.")
        sys.exit(1)


class MessageFramer:
    """
    Handles streaming TCP data buffering and extraction of newline-delimited
    JSON message frames.
    """
    def __init__(self):
        self._buffer = ""

    def feed(self, text: str):
        self._buffer += text
        messages = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                messages.append(msg)
            except json.JSONDecodeError as e:
                print(f"[Protocol Warning] Corrupted JSON frame received: {line[:50]}... ({e})")
        return messages


def format_grid(grid: list) -> str:
    """
    Formats a 10x10 matrix into the project's standard ASCII Battleship board grid.
    Columns: A-J, Rows: 01-10.
    """
    cols = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    lines = []
    header = "|00|" + "|".join(cols) + "|"
    divider = "-" * len(header)
    lines.append(header)
    lines.append(divider)
    for r in range(10):
        row_label = f"{r + 1:02d}"
        row_cells = []
        for c in range(10):
            val = grid[r][c] if r < len(grid) and c < len(grid[r]) else " "
            val_str = str(val).strip() if val is not None else " "
            row_cells.append(val_str if val_str else " ")
        lines.append(f"|{row_label}|" + "|".join(row_cells) + "|")
        lines.append(divider)
    return "\n".join(lines)


def parse_coordinate(coord_input: str) -> Optional[Tuple[str, int, int, int]]:
    """
    Parses a user input coordinate string (e.g., 'A1', 'B10', 'j5') into:
    (col_letter, row_num, col_idx, row_idx).
    Valid columns: A-J (case-insensitive)
    Valid rows: 1-10
    Returns None if the format is invalid.
    """
    cleaned = coord_input.strip().upper()
    match = re.match(r"^([A-J])\s*([0-9]|10)$", cleaned)
    if not match:
        return None
    col_letter = match.group(1)
    row_num = int(match.group(2))
    if row_num < 1 or row_num > 10:
        return None
    col_idx = ord(col_letter) - ord('A')
    row_idx = row_num - 1
    return col_letter, row_num, col_idx, row_idx


class BattleshipClient:
    """
    Client for the network Battleship game.
    Manages TCP communication, FSM state transitions, message framing,
    and user interactions.
    """
    def __init__(self, hostname: str, port: int, player_id: str = "Player"):
        self.hostname = hostname
        self.port = port
        self.player_id = player_id
        self.server_ip: Optional[str] = None
        self.sock: Optional[socket.socket] = None
        self.state = ClientState.INIT
        self.running = False
        self._listener_thread: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._framer = MessageFramer()
        self.my_turn_event = threading.Event()

    def connect(self) -> bool:
        """
        Resolves DNS hostname and connects to the server via TCP socket.
        Sends the initial CONNECT handshake message and launches receiver thread.
        """
        print(f"[*] Resolving server DNS hostname '{self.hostname}'...")
        self.server_ip = resolve_hostname(self.hostname)
        print(f"[+] DNS resolved successfully: {self.hostname} -> {self.server_ip}")

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        print(f"[*] Connecting to game server at {self.server_ip}:{self.port}...")
        try:
            self.sock.connect((self.server_ip, self.port))
        except ConnectionRefusedError:
            print(f"[Connection Error] Connection refused by {self.server_ip}:{self.port}. Is the server running?")
            return False
        except socket.timeout:
            print(f"[Connection Error] Connection to {self.server_ip}:{self.port} timed out.")
            return False
        except OSError as e:
            print(f"[Connection Error] Failed to connect to {self.server_ip}:{self.port}: {e}")
            return False

        print(f"[+] Connected to Battleship server.")
        self.running = True

        # Send initial CONNECT message per protocol specification
        self.send_message("CONNECT")
        self.state = ClientState.WAITING_FOR_PLAYERS

        # Start background listener thread for receiving server broadcasts
        self._listener_thread = threading.Thread(
            target=self._listen_loop,
            name="ClientListenerThread",
            daemon=True
        )
        self._listener_thread.start()
        return True

    def send_message(self, msg_type: str, payload: Optional[dict] = None) -> bool:
        """
        Encodes and transmits a newline-delimited JSON message to the server.
        """
        if not self.sock:
            return False

        msg = {
            "msg_type": msg_type,
            "player_id": self.player_id,
            "timestamp": int(time.time())
        }
        if payload is not None:
            msg["payload"] = payload

        try:
            wire_data = (json.dumps(msg) + "\n").encode("utf-8")
            with self._send_lock:
                self.sock.sendall(wire_data)
            return True
        except OSError as e:
            if self.running:
                print(f"[Network Error] Failed to send message '{msg_type}': {e}")
            return False

    def _listen_loop(self):
        """
        Background worker thread that reads from TCP socket and processes incoming
        JSON messages.
        """
        while self.running:
            try:
                data = self.sock.recv(4096)
                if not data:
                    if self.running:
                        print("\n[Server Teardown] Server closed the connection.")
                    break
                text = data.decode("utf-8")
                messages = self._framer.feed(text)
                for msg in messages:
                    self._handle_message(msg)
            except (ConnectionResetError, ConnectionAbortedError):
                if self.running:
                    print("\n[Server Teardown] Connection lost or reset by server.")
                break
            except OSError as e:
                if self.running:
                    print(f"\n[Socket Error] Error receiving data: {e}")
                break

        self.state = ClientState.DISCONNECTED
        self.running = False
        self.my_turn_event.set()  # Unblock main thread if waiting

    def _handle_message(self, msg: dict):
        """
        Finite State Machine transition logic based on protocol message types.
        """
        msg_type = msg.get("msg_type")
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        if msg_type == "LOBBY_WAIT":
            self.state = ClientState.WAITING_FOR_PLAYERS
            print("\n[Lobby] Waiting for opponent to join the match...")

        elif msg_type == "GAME_START":
            self.state = ClientState.GAME_START
            print("\n" + "=" * 55)
            print("         BATTLESHIP - GAME COMMENCED!            ")
            print("=" * 55)
            if "role" in payload:
                self.player_id = payload["role"]
                print(f"Role assigned by server: {self.player_id}")
            elif "player_id" in msg and msg["player_id"]:
                self.player_id = msg["player_id"]

            active_turn = (
                payload.get("turn") or 
                payload.get("first_turn") or 
                payload.get("active_player") or
                payload.get("next_turn")
            )
            if active_turn == self.player_id:
                print("Coin flip result: You have the first turn!")
                self.state = ClientState.PLAYER_TURN
                self.my_turn_event.set()
            elif active_turn:
                print(f"Coin flip result: {active_turn} has the first turn.")
                self.state = ClientState.WAITING_FOR_TURN
                self.my_turn_event.clear()

        elif msg_type == "STATE_UPDATE":
            move_res = (
                payload.get("move result") or 
                payload.get("move_result") or 
                payload.get("result")
            )
            if move_res:
                print(f"\n================ [ MOVE RESULT: {move_res.upper()} ] ================")

            # Print game boards if included in state update
            offense = payload.get("offense board") or payload.get("offense_board")
            defense = payload.get("defense board") or payload.get("defense_board")

            if offense:
                print("\n[OFFENSE BOARD] (Your strikes on enemy territory):")
                if isinstance(offense, str):
                    print(offense.strip())
                elif isinstance(offense, list):
                    print(format_grid(offense))

            if defense:
                print("\n[DEFENSE BOARD] (Your fleet):")
                if isinstance(defense, str):
                    print(defense.strip())
                elif isinstance(defense, list):
                    print(format_grid(defense))

            # Turn determination
            next_turn = (
                payload.get("next_turn") or 
                payload.get("NextTurn") or 
                payload.get("current_turn") or 
                payload.get("active_player") or 
                payload.get("turn") or
                payload.get("player_turn")
            )
            is_my_turn = (next_turn == self.player_id) or (payload.get("is_your_turn") is True)

            if is_my_turn:
                print("\n>>> It is YOUR turn! Prepare your coordinates. <<<")
                self.state = ClientState.PLAYER_TURN
                self.my_turn_event.set()
            else:
                turn_label = next_turn if next_turn else "Opponent"
                print(f"\n[Turn: {turn_label}] Awaiting opponent's strike...")
                self.state = ClientState.WAITING_FOR_TURN
                self.my_turn_event.clear()

        elif msg_type == "ERROR":
            err_code = payload.get("error_code", 400)
            err_msg = payload.get("error_message", "Invalid action.")
            print(f"\n[SERVER ERROR {err_code}]: {err_msg}")
            # Per FSM: Invalid move keeps turn with same player
            print("Please try entering a different coordinate.")
            self.state = ClientState.PLAYER_TURN
            self.my_turn_event.set()

        elif msg_type == "GAME_OVER":
            self.state = ClientState.GAME_OVER
            winner = payload.get("winner", "Unknown")
            print("\n" + "#" * 55)
            print("                    GAME OVER                    ")
            print("#" * 55)
            if winner == self.player_id:
                print(f" VICTORY! All enemy ships destroyed. You ({self.player_id}) won!")
            else:
                print(f" DEFEAT! {winner} won the battle.")
            print("#" * 55 + "\n")
            self.running = False
            self.my_turn_event.set()

        elif msg_type == "DISCONNECT":
            print("\n[Notice] Received disconnect notification from server.")
            self.state = ClientState.DISCONNECTED
            self.running = False
            self.my_turn_event.set()

        elif msg_type == "RECONNECT_PROMPT":
            print("\n[Server Alert] Disconnection detected. Server prompted for reconnect.")
            self.state = ClientState.CONNECTION_INTERRUPTED

    def run(self):
        """
        Main execution loop. Manages user CLI input during active turns.
        """
        try:
            while self.running:
                # Wait until it is this client's turn or game stops
                self.my_turn_event.wait(timeout=0.5)
                if not self.running:
                    break

                if self.state == ClientState.PLAYER_TURN and self.my_turn_event.is_set():
                    try:
                        user_input = input("Enter target coordinate (e.g. A5, J10) or 'quit': ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        self.disconnect()
                        break

                    if not user_input:
                        continue

                    if user_input.lower() in ("quit", "exit", "q"):
                        print("[*] Quitting game...")
                        self.disconnect()
                        break

                    coord = parse_coordinate(user_input)
                    if not coord:
                        print("Invalid format. Use Column A-J and Row 1-10 (e.g., A1, B5, J10).")
                        continue

                    col_letter, row_num, col_idx, row_idx = coord
                    # Construct MOVE payload supporting both protocol schemas
                    payload = {
                        "column": col_letter,
                        "row": row_num,
                        "col": col_idx,
                        "row_idx": row_idx,
                        "coordinate": f"{col_letter}{row_num}"
                    }

                    self.my_turn_event.clear()
                    self.state = ClientState.EVALUATE_MOVE
                    print(f"[*] Firing at {col_letter}{row_num}... Waiting for server evaluation.")
                    if not self.send_message("MOVE", payload):
                        print("[Error] Failed to send move to server.")
                        break

        except KeyboardInterrupt:
            print("\n[*] Interrupted by user.")
        finally:
            self.disconnect()

    def disconnect(self):
        """
        Performs graceful connection teardown and cleanup according to FSM.
        """
        if self.state != ClientState.DISCONNECTED and self.state != ClientState.CLEANUP:
            self.state = ClientState.CLEANUP
            self.running = False
            self.my_turn_event.set()

            if self.sock:
                try:
                    self.send_message("DISCONNECT")
                except Exception:
                    pass
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

            self.state = ClientState.DISCONNECTED
            print("[+] Disconnected cleanly.")


def main():
    args = parse_arguments()

    if not (1 <= args.port <= 65535):
        print(f"Error: Invalid port number {args.port}. Port must be between 1 and 65535.")
        sys.exit(1)

    client = BattleshipClient(
        hostname=args.hostname,
        port=args.port,
        player_id=args.player_id
    )

    def signal_handler(sig, frame):
        print("\n[*] Exiting...")
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    if client.connect():
        client.run()
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
