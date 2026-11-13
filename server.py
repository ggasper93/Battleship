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

Fleet Specification:
- A: Aircraft Carrier (5 slots)
- B: Battleship (4 slots)
- C: Cruiser (3 slots)
- S: Submarine (3 slots)
- D: Destroyer (2 slots)
"""

import argparse
import json
import random
import re
import signal
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
    SHIP_PLACEMENT = "SHIP_PLACEMENT"
    PLAYER_TURN = "PLAYER_TURN"
    EVALUATE_MOVE = "EVALUATE_MOVE"
    GAME_OVER = "GAME_OVER"
    CLEANUP = "CLEANUP"
    CONNECTION_INTERRUPTED = "CONNECTION_INTERRUPTED"


# Fleet Specification: 5 Ships with specific designation letters and slot sizes
FLEET_SPEC = [
    {"name": "Aircraft Carrier", "letter": "A", "size": 5},
    {"name": "Battleship", "letter": "B", "size": 4},
    {"name": "Cruiser", "letter": "C", "size": 3},
    {"name": "Submarine", "letter": "S", "size": 3},
    {"name": "Destroyer", "letter": "D", "size": 2}
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
    Ships are designated by letters:
      A: Aircraft Carrier (5 slots)
      B: Battleship (4 slots)
      C: Cruiser (3 slots)
      S: Submarine (3 slots)
      D: Destroyer (2 slots)
    """
    def __init__(self, player_id: str):
        self.player_id = player_id
        # defense_grid: 'A','B','C','S','D' = intact ships, 'X' = hit, 'M' = enemy miss, ' ' = water
        self.defense_grid: List[List[str]] = [[" " for _ in range(10)] for _ in range(10)]
        # offense_grid: 'X' = successful hit, 'M' = miss, ' ' = unexplored water
        self.offense_grid: List[List[str]] = [[" " for _ in range(10)] for _ in range(10)]
        self.ships: List[dict] = []

    def add_ship(self, name: str, letter: str, size: int, coords: List[Tuple[int, int]]) -> bool:
        """Adds a placed ship to the player's defense board."""
        for r, c in coords:
            self.defense_grid[r][c] = letter
        self.ships.append({
            "name": name,
            "letter": letter,
            "size": size,
            "coords": set(coords),
            "hits": set()
        })
        return True

    def process_incoming_shot(self, r: int, c: int) -> Tuple[str, Optional[str], Optional[str]]:
        """
        Processes an incoming shot from the opponent at (r, c).
        Returns (result, sunk_ship_name, ship_letter).
        Result is one of: 'already_targeted', 'hit', or 'miss'.
        """
        cell = self.defense_grid[r][c]
        if cell in ("X", "M"):
            return "already_targeted", None, None

        if cell in ("A", "B", "C", "S", "D"):
            hit_letter = cell
            self.defense_grid[r][c] = "X"
            sunk_name = None
            for ship in self.ships:
                if (r, c) in ship["coords"]:
                    ship["hits"].add((r, c))
                    if ship["hits"] == ship["coords"]:
                        sunk_name = ship["name"]
                    break
            return "hit", sunk_name, hit_letter
        else:
            self.defense_grid[r][c] = "M"
            return "miss", None, None

    def all_ships_sunk(self) -> bool:
        """Returns True if every cell of all 5 placed ships has been hit."""
        return len(self.ships) == 5 and all(ship["hits"] == ship["coords"] for ship in self.ships)


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
    Flexibly parses target shot coordinates from various payload formats:
    - {'column': 'A', 'row': 1} or {'column': 'A', 'row': '1'}
    - {'row': 0, 'col': 2}
    - {'coordinate': 'A1'}
    Returns (row_idx, col_idx) in 0-indexed integer range [0..9], or None if invalid.
    """
    if not isinstance(payload, dict):
        return None

    if "coordinate" in payload and isinstance(payload["coordinate"], str):
        match = re.match(r"^([A-J])\s*([0-9]|10)$", payload["coordinate"].strip().upper())
        if match:
            col_letter = match.group(1)
            row_num = int(match.group(2))
            if 1 <= row_num <= 10:
                return (row_num - 1, ord(col_letter) - ord("A"))

    if "column" in payload and "row" in payload:
        col_val = str(payload["column"]).strip().upper()
        try:
            row_val = int(payload["row"])
        except (ValueError, TypeError):
            return None

        if len(col_val) == 1 and "A" <= col_val <= "J":
            col_idx = ord(col_val) - ord("A")
            if 1 <= row_val <= 10:
                return (row_val - 1, col_idx)
            elif 0 <= row_val <= 9:
                return (row_val, col_idx)

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
    FSM states, player ship placement validation, turn control, and game execution.
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
        self.placed_players: Set[str] = set()  # Set of player_ids that have submitted valid placements
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
        # Set a short timeout on the listening socket so accept() periodically yields
        # control, allowing Python to catch KeyboardInterrupt / signals immediately on Windows & Linux
        self.server_socket.settimeout(0.5)
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
                except socket.timeout:
                    continue
                except OSError:
                    break

                # Ensure accepted client socket is blocking for worker thread communication
                conn.settimeout(None)

                worker = threading.Thread(
                    target=self._client_handler,
                    args=(conn, addr),
                    daemon=True
                )
                worker.start()

        except KeyboardInterrupt:
            print("\n[*] Server shutdown initiated by operator (Ctrl+C).")
        finally:
            self.stop()

    def stop(self):
        """Performs teardown of all client sockets and the listening socket."""
        if not self.running and self.server_socket is None:
            return
        self.running = False
        with self.state_lock:
            for pid, client_info in list(self.clients.items()):
                try:
                    client_info["sock"].shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
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

        elif msg_type == "PLACE_SHIPS":
            player_id = current_player_id or client_player_id
            self._handle_place_ships(conn, player_id, msg.get("payload", {}))
            return current_player_id

        elif msg_type == "MOVE":
            player_id = current_player_id or client_player_id
            self._handle_move(conn, player_id, msg.get("payload", {}))
            return current_player_id

        elif msg_type == "DISCONNECT":
            player_id = current_player_id or client_player_id
            print(f"[*] Voluntary DISCONNECT received from {player_id}")
            return current_player_id

        else:
            self._send_json(conn, "ERROR", client_player_id, {
                "error_code": 400,
                "error_message": f"Unsupported msg_type: '{msg_type}'"
            })
            return current_player_id

    def _handle_connect(self, conn: socket.socket, addr: Tuple[str, int], requested_id: str) -> str:
        """Handles client registration and match initialization."""
        to_notify_lobby = None
        game_start_payloads = []

        with self.state_lock:
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
                self.state = ServerState.SHIP_PLACEMENT
                print(f"[Registration] Registered {assigned_id} from {addr}. Initiating ship placement phase!")

                p1_id = self.player_order[0]
                p2_id = self.player_order[1]
                p1_sock = self.clients[p1_id]["sock"]
                p2_sock = self.clients[p2_id]["sock"]

                # Send GAME_START with instruction to place ships
                game_start_payloads = [
                    (p1_sock, "GAME_START", p1_id, {"role": p1_id, "status": "place_ships"}),
                    (p2_sock, "GAME_START", p2_id, {"role": p2_id, "status": "place_ships"})
                ]
            else:
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

        for s, mtype, pid, payload in game_start_payloads:
            self._send_json(s, mtype, pid, payload)

        return assigned_id

    def _handle_place_ships(self, conn: socket.socket, player_id: str, payload: dict):
        """Validates and registers player ship placement."""
        error_msg = None
        start_battle = False
        battle_start_broadcasts = []
        waiting_notification = None

        with self.state_lock:
            if self.state != ServerState.SHIP_PLACEMENT and self.state != ServerState.GAME_START:
                error_msg = "Game is not in the ship placement phase."
            elif player_id in self.placed_players:
                error_msg = "Fleet has already been placed for this match."
            else:
                ships_data = payload.get("ships", [])
                if not isinstance(ships_data, list) or len(ships_data) != 5:
                    error_msg = "Invalid placement: Must provide exactly 5 ships."
                else:
                    # Validate all 5 required ships and letters
                    board = PlayerBoard(player_id)
                    occupied_cells: Set[Tuple[int, int]] = set()

                    expected_ships = {s["letter"]: s for s in FLEET_SPEC}
                    placed_letters = set()

                    for ship in ships_data:
                        letter = str(ship.get("letter", "")).upper()
                        name = ship.get("name", "")
                        if letter not in expected_ships:
                            error_msg = f"Invalid ship letter '{letter}'. Must be A, B, C, S, or D."
                            break
                        if letter in placed_letters:
                            error_msg = f"Duplicate placement for ship '{letter}'."
                            break

                        spec = expected_ships[letter]
                        size = spec["size"]
                        placed_letters.add(letter)

                        # Extract coordinates list
                        raw_coords = ship.get("coords", [])
                        if len(raw_coords) != size:
                            error_msg = f"Ship '{letter}' ({spec['name']}) must occupy exactly {size} slots."
                            break

                        coords: List[Tuple[int, int]] = []
                        for item in raw_coords:
                            if isinstance(item, (list, tuple)) and len(item) == 2:
                                r, c = int(item[0]), int(item[1])
                            elif isinstance(item, dict) and "row" in item and "col" in item:
                                r, c = int(item["row"]), int(item["col"])
                            else:
                                error_msg = f"Malformed coordinate item: {item}"
                                break

                            if not (0 <= r <= 9 and 0 <= c <= 9):
                                error_msg = f"Coordinates for '{letter}' out of board bounds [0..9]."
                                break
                            if (r, c) in occupied_cells:
                                error_msg = f"Overlap detected for '{letter}' at ({r},{c})."
                                break

                            coords.append((r, c))
                            occupied_cells.add((r, c))

                        if error_msg:
                            break

                        # Check linearity and contiguity
                        rows = [r for r, c in coords]
                        cols = [c for r, c in coords]
                        is_horizontal = len(set(rows)) == 1 and sorted(cols) == list(range(min(cols), min(cols) + size))
                        is_vertical = len(set(cols)) == 1 and sorted(rows) == list(range(min(rows), min(rows) + size))

                        if not (is_horizontal or is_vertical):
                            error_msg = f"Ship '{letter}' must be placed in a contiguous straight line."
                            break

                        board.add_ship(spec["name"], letter, size, coords)

                    if not error_msg:
                        self.boards[player_id] = board
                        self.placed_players.add(player_id)
                        print(f"[Fleet Deployed] {player_id} placed all 5 ships ({len(occupied_cells)} slots).")

                        # If both players have placed ships, proceed to battle!
                        if len(self.placed_players) == 2:
                            start_battle = True
                            p1_id = self.player_order[0]
                            p2_id = self.player_order[1]

                            # Turn Mechanics: Initial turn decided via coin flip
                            self.active_turn = random.choice([p1_id, p2_id])
                            self.state = ServerState.PLAYER_TURN
                            print(f"[Coin Flip] Initial turn awarded to: {self.active_turn}")

                            p1_sock = self.clients[p1_id]["sock"]
                            p2_sock = self.clients[p2_id]["sock"]

                            battle_start_broadcasts = [
                                (p1_sock, "STATE_UPDATE", p1_id, {
                                    "move result": "All fleets deployed! Battle commences.",
                                    "offense board": format_board(self.boards[p1_id].offense_grid),
                                    "defense board": format_board(self.boards[p1_id].defense_grid),
                                    "next_turn": self.active_turn,
                                    "is_your_turn": (self.active_turn == p1_id)
                                }),
                                (p2_sock, "STATE_UPDATE", p2_id, {
                                    "move result": "All fleets deployed! Battle commences.",
                                    "offense board": format_board(self.boards[p2_id].offense_grid),
                                    "defense board": format_board(self.boards[p2_id].defense_grid),
                                    "next_turn": self.active_turn,
                                    "is_your_turn": (self.active_turn == p2_id)
                                })
                            ]
                        else:
                            waiting_notification = (conn, player_id)

        # Transmit outside state lock
        if error_msg:
            self._send_json(conn, "ERROR", player_id, {
                "error_code": 400,
                "error_message": error_msg
            })
            return

        if waiting_notification:
            self._send_json(waiting_notification[0], "LOBBY_WAIT", waiting_notification[1], {
                "message": "Fleet registered! Waiting for opponent to place ships."
            })

        if start_battle:
            for s, mtype, pid, pld in battle_start_broadcasts:
                self._send_json(s, mtype, pid, pld)

    def _handle_move(self, conn: socket.socket, player_id: str, payload: dict):
        """Processes player moves, evaluates hits/misses, updates FSM state and broadcasts."""
        broadcast_items = []
        error_msg = None

        with self.state_lock:
            if self.state != ServerState.PLAYER_TURN:
                error_msg = "Game is not in active play (ships may still be deploying)."
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

                    if attacker_board.offense_grid[r][c] in ("X", "M"):
                        error_msg = "Invalid move: Cell already guessed."
                    else:
                        col_letter = chr(ord("A") + c)
                        row_label = r + 1
                        coord_str = f"{col_letter}{row_label}"

                        shot_result, sunk_name, hit_letter = defender_board.process_incoming_shot(r, c)

                        if shot_result == "hit":
                            attacker_board.offense_grid[r][c] = "X"
                            if sunk_name:
                                move_result_str = f"HIT! You sank the enemy's {sunk_name} ({hit_letter}) at {coord_str}!"
                                opp_result_str = f"ENEMY HIT! Your {sunk_name} ({hit_letter}) was SUNK at {coord_str}!"
                            else:
                                move_result_str = f"HIT on enemy ship ({hit_letter}) at {coord_str}!"
                                opp_result_str = f"ENEMY HIT on your fleet ({hit_letter}) at {coord_str}!"
                        else:
                            attacker_board.offense_grid[r][c] = "M"
                            move_result_str = f"MISS at {coord_str}."
                            opp_result_str = f"Enemy shot at {coord_str} was a MISS."

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
            try:
                conn.close()
            except Exception:
                pass

            if player_id and player_id in self.clients:
                del self.clients[player_id]
                print(f"[*] Removed {player_id} from active client registry.")

            if self.state in (ServerState.PLAYER_TURN, ServerState.GAME_START, ServerState.SHIP_PLACEMENT, ServerState.EVALUATE_MOVE):
                self.state = ServerState.GAME_OVER
                if self.clients:
                    rem_id = next(iter(self.clients.keys()))
                    remaining_client = (self.clients[rem_id]["sock"], rem_id)
                print(f"[Forfeit] Player {player_id} disconnected during game.")

            if len(self.clients) == 0 or self.state == ServerState.GAME_OVER:
                self.state = ServerState.CLEANUP
                self.boards.clear()
                self.placed_players.clear()
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

    def signal_handler(sig, frame):
        print("\n[*] Server shutdown initiated by operator (Ctrl+C).")
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGBREAK"):  # Windows Ctrl+Break support
        signal.signal(signal.SIGBREAK, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    server.start()


if __name__ == "__main__":
    main()
