"""
Battleship Game Server
CS 457 - Computer Networks
Author: Garret Gasper

This module implements the server-side networking, game state coordination,
and concurrency architecture for the multiplayer Battleship network game.

Architecture & Concurrency (Option A: Multi-Threading):
- Main thread listens on 0.0.0.0:<port> and accepts incoming TCP client connections.
- A dedicated worker thread is spawned for each connected client socket.
- Shared memory structures (game state, board matrices, client registry, turn tracker)
  are synchronized using a threading.Lock to prevent race conditions during state mutation.
- Network transmissions occur outside of lock acquisition to maintain responsiveness.
- Framing: Newline-delimited ('\\n') JSON payloads conforming to protocol_blueprint.md.
"""

import argparse
import json
import random
import re
import socket
import sys
import threading
import time
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple


class ServerState(str, Enum):
    """Server finite state machine states per fsm_specification.md."""
    INIT = "INIT"
    WAITING_FOR_PLAYERS = "WAITING_FOR_PLAYERS"
    GAME_START = "GAME_START"
    PLAYER_TURN = "PLAYER_TURN"
    EVALUATE_MOVE = "EVALUATE_MOVE"
    GAME_OVER = "GAME_OVER"
    CLEANUP = "CLEANUP"
    CONNECTION_INTERRUPTED = "CONNECTION_INTERRUPTED"


# Standard Battleship Fleet Configuration (5 Ships)
FLEET_SPEC = [
    {"name": "Carrier", "size": 5},
    {"name": "Battleship", "size": 4},
    {"name": "Cruiser", "size": 3},
    {"name": "Submarine", "size": 3},
    {"name": "Destroyer", "size": 2}
]


def format_board(grid: List[List[str]]) -> str:
    """
    Renders a 10x10 matrix into the project's standard ASCII Battleship board grid.
    Columns: A-J, Rows: 01-10.
    """
    cols = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    lines = []
    lines.append("|00|" + "|".join(cols) + "|")
    lines.append("-------------------------")
    for r in range(10):
        row_label = f"{r + 1:02d}"
        row_cells = [str(grid[r][c]) if str(grid[r][c]).strip() else " " for c in range(10)]
        lines.append(f"|{row_label}|" + "|".join(row_cells) + "|")
        lines.append("-------------------------")
    return "\n".join(lines)


class PlayerBoard:
    """
    Manages defense (fleet placement & damage) and offense (targeted cells)
    for a single player on 10x10 coordinate grids.
    """
    def __init__(self, player_id: str):
        self.player_id = player_id
        # defense_grid: 'S' = intact ship, 'X' = damaged ship, 'M' = enemy miss, ' ' = water
        self.defense_grid: List[List[str]] = [[" " for _ in range(10)] for _ in range(10)]
        # offense_grid: 'X' = successful hit, 'M' = miss, ' ' = unexplored water
        self.offense_grid: List[List[str]] = [[" " for _ in range(10)] for _ in range(10)]
        self.ships: List[dict] = []
        self._place_fleet_randomly()

    def _place_fleet_randomly(self):
        """Randomly generates valid, non-overlapping placements for all 5 fleet ships."""
        for spec in FLEET_SPEC:
            name = spec["name"]
            size = spec["size"]
            placed = False
            attempts = 0
            while not placed and attempts < 2000:
                attempts += 1
                orientation = random.choice(["H", "V"])
                if orientation == "H":
                    r = random.randint(0, 9)
                    c = random.randint(0, 10 - size)
                    coords = {(r, c + i) for i in range(size)}
                else:
                    r = random.randint(0, 10 - size)
                    c = random.randint(0, 9)
                    coords = {(r + i, c) for i in range(size)}

                # Check overlap against existing ships
                if not any(coords & s["coords"] for s in self.ships):
                    for cr, cc in coords:
                        self.defense_grid[cr][cc] = "S"
                    self.ships.append({
                        "name": name,
                        "coords": coords,
                        "hits": set()
                    })
                    placed = True

    def process_incoming_shot(self, r: int, c: int) -> Tuple[str, Optional[str]]:
        """
        Processes an incoming shot from the opponent at (r, c).
        Returns (result, sunk_ship_name), where result is 'already_targeted', 'hit', or 'miss'.
        """
        cell = self.defense_grid[r][c]
        if cell in ("X", "M"):
            return "already_targeted", None

        if cell == "S":
            self.defense_grid[r][c] = "X"
            sunk_ship = None
            for ship in self.ships:
                if (r, c) in ship["coords"]:
                    ship["hits"].add((r, c))
                    if ship["hits"] == ship["coords"]:
                        sunk_ship = ship["name"]
                    break
            return "hit", sunk_ship
        else:
            self.defense_grid[r][c] = "M"
            return "miss", None

    def all_ships_sunk(self) -> bool:
        """Returns True if every cell of every ship has been hit."""
        return all(ship["hits"] == ship["coords"] for ship in self.ships)


class MessageFramer:
    """Extracts newline-delimited JSON messages from a streaming TCP socket buffer."""
    def __init__(self):
        self._buffer = ""

    def feed(self, text: str) -> List[dict]:
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
                print(f"[Warning] Failed to parse JSON message: {line[:50]} ({e})")
        return messages


def extract_coordinates(payload: dict) -> Optional[Tuple[int, int]]:
    """
    Flexibly parses coordinates from various protocol payload schemas:
    - {'column': 'A', 'row': 1} or {'column': 'A', 'row': '1'}
    - {'row': 0, 'col': 2}
    - {'coordinate': 'A1'}
    Returns (row_idx, col_idx) in 0-indexed integer range [0..9], or None if invalid.
    """
    if not isinstance(payload, dict):
        return None

    # Check coordinate string (e.g. 'A1', 'J10')
    if "coordinate" in payload and isinstance(payload["coordinate"], str):
        match = re.match(r"^([A-J])\s*([0-9]|10)$", payload["coordinate"].strip().upper())
        if match:
            col_letter = match.group(1)
            row_num = int(match.group(2))
            if 1 <= row_num <= 10:
                return (row_num - 1, ord(col_letter) - ord("A"))

    # Check column + row (e.g. column: "A", row: 1 or "1")
    if "column" in payload and "row" in payload:
        col_val = str(payload["column"]).strip().upper()
        try:
            row_val = int(payload["row"])
        except (ValueError, TypeError):
            return None

        if len(col_val) == 1 and "A" <= col_val <= "J":
            col_idx = ord(col_val) - ord("A")
            # Row could be 1-indexed (1..10) or 0-indexed (0..9)
            if 1 <= row_val <= 10:
                row_idx = row_val - 1
                return (row_idx, col_idx)
            elif 0 <= row_val <= 9:
                return (row_val, col_idx)

    # Check row + col integers
    if "row" in payload and "col" in payload:
        try:
            r = int(payload["row"])
            c = int(payload["col"])
            if 0 <= r <= 9 and 0 <= c <= 9:
                return (r, c)
            elif 1 <= r <= 10 and 1 <= c <= 10:
                return (r - 1, c - 1)
        except (ValueError, TypeError):
            pass

    return None


class BattleshipServer:
    """
    Multi-threaded Battleship server managing TCP connections, synchronization,
    FSM states, turn validation, and game loop execution.
    """
    def __init__(self, port: int):
        self.port = port
        self.server_socket: Optional[socket.socket] = None
        self.running = False

        # Shared synchronization lock
        self.state_lock = threading.Lock()

        # Shared game state resources
        self.state = ServerState.INIT
        self.clients: Dict[str, dict] = {}  # {player_id: {"sock": socket, "addr": addr, "role": role}}
        self.boards: Dict[str, PlayerBoard] = {}  # {player_id: PlayerBoard}
        self.active_turn: Optional[str] = None
        self.player_order: List[str] = []

    def start(self):
        """Initializes TCP server socket bound to 0.0.0.0:<port> and enters accept loop."""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.server_socket.bind(("0.0.0.0", self.port))
        except OSError as e:
            print(f"[Fatal] Unable to bind to 0.0.0.0:{self.port}: {e}")
            sys.exit(1)

        self.server_socket.listen(5)
        self.running = True
        self.state = ServerState.WAITING_FOR_PLAYERS

        print(f"=======================================================")
        print(f" Battleship Server listening on 0.0.0.0:{self.port}")
        print(f" State: {self.state}")
        print(f"=======================================================")

        try:
            while self.running:
                try:
                    conn, addr = self.server_socket.accept()
                except OSError:
                    break

                # Spawn worker thread for each connected client
                worker = threading.Thread(
                    target=self._client_handler,
                    args=(conn, addr),
                    daemon=True
                )
                worker.start()

        except KeyboardInterrupt:
            print("\n[*] Server shutdown initiated by operator.")
        finally:
            self.stop()

    def stop(self):
        """Performs teardown of all client sockets and the listening socket."""
        self.running = False
        with self.state_lock:
            for pid, client_info in list(self.clients.items()):
                try:
                    client_info["sock"].close()
                except Exception:
                    pass
            self.clients.clear()

        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
            self.server_socket = None
        print("[+] Server stopped cleanly.")

    def _send_json(self, sock: socket.socket, msg_type: str, player_id: str, payload: Optional[dict] = None) -> bool:
        """Transmits a newline-delimited JSON payload to a client socket (outside state lock)."""
        msg = {
            "msg_type": msg_type,
            "player_id": player_id,
            "timestamp": int(time.time())
        }
        if payload is not None:
            msg["payload"] = payload

        try:
            wire_bytes = (json.dumps(msg) + "\n").encode("utf-8")
            sock.sendall(wire_bytes)
            return True
        except OSError as e:
            print(f"[Warning] Failed to send {msg_type} to {player_id}: {e}")
            return False

    def _client_handler(self, conn: socket.socket, addr: Tuple[str, int]):
        """Dedicated worker thread servicing an active client connection."""
        print(f"[+] Incoming connection accepted from {addr[0]}:{addr[1]}")
        framer = MessageFramer()
        assigned_id: Optional[str] = None

        try:
            while self.running:
                data = conn.recv(4096)
                if not data:
                    print(f"[-] Client {addr[0]}:{addr[1]} disconnected (EOF).")
                    break

                text = data.decode("utf-8")
                messages = framer.feed(text)
                for msg in messages:
                    assigned_id = self._process_client_message(conn, addr, msg, assigned_id)

        except (ConnectionResetError, ConnectionAbortedError):
            print(f"[-] Connection reset by {addr[0]}:{addr[1]}")
        except Exception as e:
            print(f"[Error] Exception in client handler for {addr}: {e}")
        finally:
            self._handle_client_teardown(conn, assigned_id)

    def _process_client_message(
        self,
        conn: socket.socket,
        addr: Tuple[str, int],
        msg: dict,
        current_player_id: Optional[str]
    ) -> Optional[str]:
        """Dispatches incoming client messages according to protocol and FSM."""
        msg_type = msg.get("msg_type")
        client_player_id = msg.get("player_id", "Player")

        if msg_type == "CONNECT":
            return self._handle_connect(conn, addr, client_player_id)

        elif msg_type == "MOVE":
            player_id = current_player_id or client_player_id
            self._handle_move(conn, player_id, msg.get("payload", {}))
            return current_player_id

        elif msg_type == "DISCONNECT":
            player_id = current_player_id or client_player_id
            print(f"[*] Voluntary DISCONNECT received from {player_id}")
            return current_player_id

        else:
            # Unknown message type
            self._send_json(conn, "ERROR", client_player_id, {
                "error_code": 400,
                "error_message": f"Unsupported msg_type: '{msg_type}'"
            })
            return current_player_id

    def _handle_connect(self, conn: socket.socket, addr: Tuple[str, int], requested_id: str) -> str:
        """Handles client registration and game start triggers."""
        start_game = False
        to_notify_lobby = None
        game_start_payloads = []

        with self.state_lock:
            # Assign player role (Player_1 or Player_2)
            if len(self.clients) == 0:
                assigned_id = "Player_1"
                self.clients[assigned_id] = {"sock": conn, "addr": addr, "role": assigned_id}
                self.player_order = [assigned_id]
                self.state = ServerState.WAITING_FOR_PLAYERS
                to_notify_lobby = (conn, assigned_id)
                print(f"[Registration] Registered {assigned_id} from {addr}. Waiting for Player 2.")

            elif len(self.clients) == 1:
                assigned_id = "Player_2"
                self.clients[assigned_id] = {"sock": conn, "addr": addr, "role": assigned_id}
                self.player_order.append(assigned_id)
                self.state = ServerState.GAME_START
                print(f"[Registration] Registered {assigned_id} from {addr}. Starting match!")

                # Initialize game boards
                p1_id = self.player_order[0]
                p2_id = self.player_order[1]
                self.boards[p1_id] = PlayerBoard(p1_id)
                self.boards[p2_id] = PlayerBoard(p2_id)

                # Turn mechanics: Initial turn decided via coin flip
                self.active_turn = random.choice([p1_id, p2_id])
                print(f"[Coin Flip] First turn awarded to: {self.active_turn}")

                # Prepare GAME_START and initial STATE_UPDATE payloads
                start_game = True
                p1_sock = self.clients[p1_id]["sock"]
                p2_sock = self.clients[p2_id]["sock"]

                game_start_payloads = [
                    (p1_sock, "GAME_START", p1_id, {"role": p1_id, "turn": self.active_turn}),
                    (p2_sock, "GAME_START", p2_id, {"role": p2_id, "turn": self.active_turn}),
                    (p1_sock, "STATE_UPDATE", p1_id, {
                        "move result": "Game started! Fleet positioned.",
                        "offense board": format_board(self.boards[p1_id].offense_grid),
                        "defense board": format_board(self.boards[p1_id].defense_grid),
                        "next_turn": self.active_turn,
                        "is_your_turn": (self.active_turn == p1_id)
                    }),
                    (p2_sock, "STATE_UPDATE", p2_id, {
                        "move result": "Game started! Fleet positioned.",
                        "offense board": format_board(self.boards[p2_id].offense_grid),
                        "defense board": format_board(self.boards[p2_id].defense_grid),
                        "next_turn": self.active_turn,
                        "is_your_turn": (self.active_turn == p2_id)
                    })
                ]
                self.state = ServerState.PLAYER_TURN
            else:
                # Lobby full (game only supports 2 players)
                assigned_id = f"Observer_{len(self.clients) + 1}"
                print(f"[Lobby Full] Connection from {addr} rejected: 2-player capacity reached.")
                self._send_json(conn, "ERROR", assigned_id, {
                    "error_code": 403,
                    "error_message": "Game room full. Maximum 2 players permitted."
                })
                conn.close()
                return assigned_id

        # Socket transmissions performed outside state lock
        if to_notify_lobby:
            self._send_json(to_notify_lobby[0], "LOBBY_WAIT", to_notify_lobby[1])

        if start_game:
            for s, mtype, pid, payload in game_start_payloads:
                self._send_json(s, mtype, pid, payload)

        return assigned_id

    def _handle_move(self, conn: socket.socket, player_id: str, payload: dict):
        """Processes player moves, evaluates hits/misses, updates FSM state and broadcasts."""
        broadcast_items = []
        error_msg = None

        with self.state_lock:
            # 1. State Validation
            if self.state != ServerState.PLAYER_TURN:
                error_msg = "Game is not in active play."
            # 2. Turn Validation
            elif player_id != self.active_turn:
                error_msg = f"Invalid action: It is not your turn (Current turn: {self.active_turn})."
            else:
                coords = extract_coordinates(payload)
                if coords is None:
                    error_msg = "Invalid move: Malformed coordinates. Use Column A-J and Row 1-10."
                else:
                    r, c = coords
                    attacker_id = player_id
                    defender_id = self.player_order[1] if attacker_id == self.player_order[0] else self.player_order[0]
                    attacker_board = self.boards[attacker_id]
                    defender_board = self.boards[defender_id]

                    # 3. Check if cell was already targeted
                    if attacker_board.offense_grid[r][c] in ("X", "M"):
                        error_msg = "Invalid move: Cell already guessed."
                    else:
                        # 4. Valid target shot: process shot
                        col_letter = chr(ord("A") + c)
                        row_label = r + 1
                        coord_str = f"{col_letter}{row_label}"

                        shot_result, sunk_name = defender_board.process_incoming_shot(r, c)

                        if shot_result == "hit":
                            attacker_board.offense_grid[r][c] = "X"
                            if sunk_name:
                                move_result_str = f"HIT! You sank the enemy's {sunk_name} at {coord_str}!"
                                opp_result_str = f"ENEMY HIT! Your {sunk_name} was SUNK at {coord_str}!"
                            else:
                                move_result_str = f"HIT at {coord_str}!"
                                opp_result_str = f"ENEMY HIT at {coord_str}!"
                        else:
                            attacker_board.offense_grid[r][c] = "M"
                            move_result_str = f"MISS at {coord_str}."
                            opp_result_str = f"Enemy shot at {coord_str} was a MISS."

                        # 5. Check victory condition
                        if defender_board.all_ships_sunk():
                            self.state = ServerState.GAME_OVER
                            winner = attacker_id
                            print(f"[Victory] {winner} destroyed all enemy ships!")

                            attacker_sock = self.clients[attacker_id]["sock"]
                            defender_sock = self.clients[defender_id]["sock"]

                            broadcast_items.extend([
                                (attacker_sock, "STATE_UPDATE", attacker_id, {
                                    "move result": move_result_str,
                                    "offense board": format_board(attacker_board.offense_grid),
                                    "defense board": format_board(attacker_board.defense_grid),
                                    "next_turn": None
                                }),
                                (defender_sock, "STATE_UPDATE", defender_id, {
                                    "move result": opp_result_str,
                                    "offense board": format_board(defender_board.offense_grid),
                                    "defense board": format_board(defender_board.defense_grid),
                                    "next_turn": None
                                }),
                                (attacker_sock, "GAME_OVER", attacker_id, {"winner": winner}),
                                (defender_sock, "GAME_OVER", defender_id, {"winner": winner})
                            ])
                        else:
                            # Toggle active turn
                            self.active_turn = defender_id
                            print(f"[Turn Shift] {attacker_id} fired at {coord_str} ({shot_result.upper()}). Next: {self.active_turn}")

                            attacker_sock = self.clients[attacker_id]["sock"]
                            defender_sock = self.clients[defender_id]["sock"]

                            broadcast_items.extend([
                                (attacker_sock, "STATE_UPDATE", attacker_id, {
                                    "move result": move_result_str,
                                    "offense board": format_board(attacker_board.offense_grid),
                                    "defense board": format_board(attacker_board.defense_grid),
                                    "next_turn": self.active_turn,
                                    "is_your_turn": False
                                }),
                                (defender_sock, "STATE_UPDATE", defender_id, {
                                    "move result": opp_result_str,
                                    "offense board": format_board(defender_board.offense_grid),
                                    "defense board": format_board(defender_board.defense_grid),
                                    "next_turn": self.active_turn,
                                    "is_your_turn": True
                                })
                            ])

        # Socket transmissions outside lock
        if error_msg:
            self._send_json(conn, "ERROR", player_id, {
                "error_code": 400,
                "error_message": error_msg
            })
            return

        for s, mtype, pid, payload in broadcast_items:
            self._send_json(s, mtype, pid, payload)

    def _handle_client_teardown(self, conn: socket.socket, player_id: Optional[str]):
        """Cleans up disconnected client resources and handles forfeiture if in progress."""
        remaining_client = None

        with self.state_lock:
            # Close socket
            try:
                conn.close()
            except Exception:
                pass

            if player_id and player_id in self.clients:
                del self.clients[player_id]
                print(f"[*] Removed {player_id} from active client registry.")

            # If game was in progress and someone disconnected, remaining player wins
            if self.state in (ServerState.PLAYER_TURN, ServerState.GAME_START, ServerState.EVALUATE_MOVE):
                self.state = ServerState.GAME_OVER
                if self.clients:
                    rem_id = next(iter(self.clients.keys()))
                    remaining_client = (self.clients[rem_id]["sock"], rem_id)
                print(f"[Forfeit] Player {player_id} disconnected during game.")

            # Reset game if both or active players gone
            if len(self.clients) == 0 or self.state == ServerState.GAME_OVER:
                self.state = ServerState.CLEANUP
                self.boards.clear()
                self.player_order.clear()
                self.active_turn = None
                self.state = ServerState.WAITING_FOR_PLAYERS
                print("[Cleanup] Game reset. Server waiting for new players.")

        if remaining_client:
            self._send_json(remaining_client[0], "GAME_OVER", remaining_client[1], {
                "winner": remaining_client[1],
                "reason": f"Opponent ({player_id}) disconnected. Victory by forfeit."
            })


def parse_arguments() -> argparse.Namespace:
    """
    Parses command-line arguments using argparse.
    Requires:
      -p / --port: Server port to listen on (binds to 0.0.0.0)
    """
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="Multiplayer Battleship Network Game Server (CS 457)",
        add_help=True
    )
    parser.add_argument(
        "-p", "--port",
        dest="port",
        required=True,
        type=int,
        metavar="PORT",
        help="Port number to listen on (required; binds to 0.0.0.0)"
    )
    return parser.parse_args()


def main():
    args = parse_arguments()

    if not (1 <= args.port <= 65535):
        print(f"Error: Invalid port number {args.port}. Port must be between 1 and 65535.")
        sys.exit(1)

    server = BattleshipServer(port=args.port)
    server.start()


if __name__ == "__main__":
    main()
