"""
Battleship Game Client
CS 457 - Computer Networks
Author: Garret Gasper

This module handles client-side networking and game logic for a multiplayer
Battleship game played over TCP with newline-delimited JSON payloads.

Architecture & Concurrency:
- Finite State Machine (FSM): INIT -> WAITING_FOR_PLAYERS -> GAME_START ->
  SHIP_PLACEMENT -> PLAYER_TURN / WAITING_FOR_TURN -> EVALUATE_MOVE -> GAME_OVER -> CLEANUP.
- Concurrency: Background listener thread receives and frames streaming JSON
  messages from the server, while the main thread manages user placement input,
  turn coordination, and attack coordinates.

Fleet Specification:
- A: Aircraft Carrier (5 slots)
- B: Battleship (4 slots)
- C: Cruiser (3 slots)
- S: Submarine (3 slots)
- D: Destroyer (2 slots)

UI & Visuals:
- Players can select Manual or Random fleet deployment.
- Board coloring: Hits ('X') in Red, Misses ('M') in Blue, Ships ('A','B','C','S','D') in Green.
- Move results are rendered directly below the last board update for clarity.
"""

import argparse
import json
import os
import random
import re
import signal
import socket
import sys
import threading
import time
from enum import Enum
from typing import List, Optional, Tuple

# Enable ANSI escape code processing on Windows terminals
if sys.platform == "win32":
    try:
        os.system("")
    except Exception:
        pass

# ANSI Color & Style Codes
COLOR_RED = "\033[91m"
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_BLUE = "\033[94m"
COLOR_CYAN = "\033[96m"
COLOR_BOLD = "\033[1m"
COLOR_RESET = "\033[0m"


class ClientState(str, Enum):
    """Client finite state machine states."""
    INIT = "INIT"
    WAITING_FOR_PLAYERS = "WAITING_FOR_PLAYERS"
    GAME_START = "GAME_START"
    SHIP_PLACEMENT = "SHIP_PLACEMENT"
    PLAYER_TURN = "PLAYER_TURN"
    WAITING_FOR_TURN = "WAITING_FOR_TURN"
    EVALUATE_MOVE = "EVALUATE_MOVE"
    GAME_OVER = "GAME_OVER"
    CONNECTION_INTERRUPTED = "CONNECTION_INTERRUPTED"
    CLEANUP = "CLEANUP"
    DISCONNECTED = "DISCONNECTED"


# Fleet Specification: 5 Ships with specific designation letters and slot sizes
FLEET_SPEC = [
    {"name": "Aircraft Carrier", "letter": "A", "size": 5},
    {"name": "Battleship", "letter": "B", "size": 4},
    {"name": "Cruiser", "letter": "C", "size": 3},
    {"name": "Submarine", "letter": "S", "size": 3},
    {"name": "Destroyer", "letter": "D", "size": 2}
]


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


def format_grid(grid: list, colorize: bool = True) -> str:
    """
    Formats a 10x10 matrix into the project's standard ASCII Battleship board grid.
    Columns: A-J, Rows: 01-10.
    Applies colors when colorize is True (Hits 'X' in Red, Misses 'M' in Blue, Ships in Green).
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
            if not val_str:
                cell_display = " "
            elif colorize:
                if val_str == "X":
                    cell_display = f"{COLOR_RED}{COLOR_BOLD}X{COLOR_RESET}"
                elif val_str == "M":
                    cell_display = f"{COLOR_BLUE}M{COLOR_RESET}"
                elif val_str in ("A", "B", "C", "S", "D"):
                    cell_display = f"{COLOR_GREEN}{val_str}{COLOR_RESET}"
                else:
                    cell_display = val_str
            else:
                cell_display = val_str
            row_cells.append(cell_display)
        lines.append(f"|{row_label}|" + "|".join(row_cells) + "|")
        lines.append(divider)
    return "\n".join(lines)


def colorize_board_str(board_text: str) -> str:
    """
    Colorizes an ASCII board string received from the server.
    Preserves column headers while coloring:
      - 'X' (Hits) in Bright Bold Red
      - 'M' (Misses) in Bright Blue
      - 'A','B','C','S','D' (Ships) in Green
    """
    if not board_text:
        return ""
    lines = board_text.strip().split("\n")
    colored_lines = []
    for line in lines:
        if line.startswith("|00|"):
            colored_lines.append(line)
        elif line.startswith("|") and len(line) > 3 and line[1:3].isdigit():
            def repl(m):
                c = m.group(1)
                if c == "X":
                    return f"|{COLOR_RED}{COLOR_BOLD}X{COLOR_RESET}"
                elif c == "M":
                    return f"|{COLOR_BLUE}M{COLOR_RESET}"
                elif c in ("A", "B", "C", "S", "D"):
                    return f"|{COLOR_GREEN}{c}{COLOR_RESET}"
                return f"|{c}"
            colored_line = re.sub(r"\|([XAMBCSD])(?=\|)", repl, line)
            colored_lines.append(colored_line)
        else:
            colored_lines.append(line)
    return "\n".join(colored_lines)


def parse_coordinate(coord_input: str) -> Optional[Tuple[str, int, int, int]]:
    """
    Parses a target strike coordinate string (e.g. 'A1', 'B10', 'j5') into:
    (col_letter, row_num, col_idx, row_idx).
    Valid columns: A-J (case-insensitive), Valid rows: 1-10.
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


def parse_placement_input(user_str: str) -> Optional[Tuple[str, int, str]]:
    """
    Parses a ship placement string like 'A1 H', 'b3 v', 'C5 horizontal', 'D4, V', 'A1'.
    Returns (col_letter, row_num, orientation), where:
      col_letter is 'A'-'J',
      row_num is 1-10,
      orientation is 'H' or 'V' (or '' if not specified in input).
    Returns None if format is invalid.
    """
    cleaned = user_str.strip()
    match = re.match(r"^([A-Ja-j])\s*([0-9]|10)\s*[,;\-\s]?\s*([HhVv](?:orizontal|ertical)?|[HhVv])?$", cleaned)
    if not match:
        return None
    col = match.group(1).upper()
    row = int(match.group(2))
    if row < 1 or row > 10:
        return None
    orient_raw = match.group(3)
    orientation = ""
    if orient_raw:
        orientation = "H" if orient_raw[0].upper() == "H" else "V"
    return col, row, orientation


class BattleshipClient:
    """
    Client for the network Battleship game.
    Manages TCP communication, FSM state transitions, fleet placement options,
    board colorization, and attack turn interactions.
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

        # Thread synchronization events
        self.placement_event = threading.Event()
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
        """Encodes and transmits a newline-delimited JSON message to the server."""
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
        """Background worker thread that reads from TCP socket and processes incoming JSON messages."""
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
        self.placement_event.set()
        self.my_turn_event.set()

    def _handle_message(self, msg: dict):
        """Finite State Machine transition logic based on protocol message types."""
        msg_type = msg.get("msg_type")
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        if msg_type == "LOBBY_WAIT":
            self.state = ClientState.WAITING_FOR_PLAYERS
            msg_desc = payload.get("message", "Waiting for opponent to join the match...")
            print(f"\n[Lobby] {msg_desc}")

        elif msg_type == "GAME_START":
            self.state = ClientState.SHIP_PLACEMENT
            print("\n" + "=" * 55)
            print("         BATTLESHIP - GAME COMMENCED!            ")
            print("=" * 55)
            if "role" in payload:
                self.player_id = payload["role"]
                print(f"Role assigned by server: {self.player_id}")
            elif "player_id" in msg and msg["player_id"]:
                self.player_id = msg["player_id"]

            print("[*] Match initiated! Proceeding to fleet deployment phase...")
            self.placement_event.set()

        elif msg_type == "STATE_UPDATE":
            move_res = (
                payload.get("move result") or 
                payload.get("move_result") or 
                payload.get("result")
            )

            offense = payload.get("offense board") or payload.get("offense_board")
            defense = payload.get("defense board") or payload.get("defense_board")

            # 1. Display Offense Board
            if offense:
                print("\n[OFFENSE BOARD] (Your strikes on enemy territory):")
                if isinstance(offense, str):
                    print(colorize_board_str(offense))
                elif isinstance(offense, list):
                    print(format_grid(offense, colorize=True))

            # 2. Display Defense Board (the last board update)
            if defense:
                print("\n[DEFENSE BOARD] (Your fleet):")
                if isinstance(defense, str):
                    print(colorize_board_str(defense))
                elif isinstance(defense, list):
                    print(format_grid(defense, colorize=True))

            # 3. Display Move Result BELOW the last board update for enhanced visibility
            if move_res:
                res_upper = move_res.upper()
                if "HIT" in res_upper or "SUNK" in res_upper:
                    colored_res = f"{COLOR_RED}{COLOR_BOLD}{res_upper}{COLOR_RESET}"
                elif "MISS" in res_upper:
                    colored_res = f"{COLOR_BLUE}{res_upper}{COLOR_RESET}"
                else:
                    colored_res = res_upper
                print(f"\n================ [ MOVE RESULT: {colored_res} ] ================")

            # 4. Determine turn
            next_turn = (
                payload.get("next_turn") or 
                payload.get("NextTurn") or 
                payload.get("current_turn") or 
                payload.get("active_player") or 
                payload.get("turn") or
                payload.get("player_turn")
            )
            if next_turn:
                is_my_turn = (next_turn == self.player_id) or (payload.get("is_your_turn") is True)
                if is_my_turn:
                    print(f"\n>>> It is YOUR turn ({self.player_id})! Prepare your coordinates. <<<")
                    self.state = ClientState.PLAYER_TURN
                    self.my_turn_event.set()
                else:
                    turn_label = next_turn
                    print(f"\n[Turn: {turn_label}] Awaiting opponent's strike...")
                    self.state = ClientState.WAITING_FOR_TURN
                    self.my_turn_event.clear()

        elif msg_type == "ERROR":
            err_code = payload.get("error_code", 400)
            err_msg = payload.get("error_message", "Invalid action.")
            print(f"\n[SERVER ERROR {err_code}]: {err_msg}")
            if self.state == ClientState.EVALUATE_MOVE:
                print("Please try entering a different coordinate.")
                self.state = ClientState.PLAYER_TURN
                self.my_turn_event.set()

        elif msg_type == "GAME_OVER":
            self.state = ClientState.GAME_OVER
            winner = payload.get("winner", "Unknown")
            reason = payload.get("reason", "")
            print("\n" + "#" * 55)
            print("                    GAME OVER                    ")
            print("#" * 55)
            if winner == self.player_id:
                print(f" {COLOR_GREEN}{COLOR_BOLD}VICTORY! All enemy ships destroyed. You ({self.player_id}) won!{COLOR_RESET}")
            else:
                print(f" {COLOR_RED}DEFEAT! {winner} won the battle.{COLOR_RESET}")
            if reason:
                print(f" Reason: {reason}")
            print("#" * 55 + "\n")
            self.running = False
            self.disconnect()
            print("[*] Game concluded. Exiting...")
            sys.stdout.flush()
            sys.stderr.flush()
            time.sleep(0.1)
            os._exit(0)

        elif msg_type == "DISCONNECT":
            print("\n[Notice] Received disconnect notification from server.")
            self.state = ClientState.DISCONNECTED
            self.running = False
            self.disconnect()
            sys.stdout.flush()
            sys.stderr.flush()
            time.sleep(0.1)
            os._exit(0)

        elif msg_type == "RECONNECT_PROMPT":
            print("\n[Server Alert] Disconnection detected. Server prompted for reconnect.")
            self.state = ClientState.CONNECTION_INTERRUPTED

    def _deploy_fleet(self) -> Optional[List[dict]]:
        """
        Offers the player the choice between manual and random ship placement.
        - [M] Manual: Player positions each of the 5 ships in order.
        - [R] Random: Fleet positions are automatically generated.
        Returns the list of 5 placed ship dictionaries, or None if user quits.
        """
        print("\n" + "=" * 55)
        print("               FLEET DEPLOYMENT OPTIONS                ")
        print("=" * 55)
        print("How would you like to position your fleet?")
        print("  [M] Manual Placement  (Position each of the 5 ships in order)")
        print("  [R] Random Placement  (Auto-generate valid fleet positions)")
        print("=" * 55)

        while True:
            try:
                choice = input("Enter deployment mode ([M]anual / [R]andom) or 'quit': ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                self.disconnect()
                return None

            if choice in ("quit", "exit", "q"):
                self.disconnect()
                return None
            elif choice in ("r", "random", "auto"):
                return self._place_ships_randomly()
            elif choice in ("m", "manual", ""):
                return self._place_ships_manually()
            else:
                print("Invalid selection. Please enter 'M' for manual or 'R' for random.")

    def _place_ships_randomly(self) -> List[dict]:
        """Automatically generates valid, non-overlapping positions for all 5 fleet ships."""
        local_grid = [[" " for _ in range(10)] for _ in range(10)]
        placed_ships = []

        for spec in FLEET_SPEC:
            name = spec["name"]
            letter = spec["letter"]
            size = spec["size"]
            while True:
                orient = random.choice(["H", "V"])
                if orient == "H":
                    r = random.randint(0, 9)
                    c = random.randint(0, 10 - size)
                    coords = [(r, c + i) for i in range(size)]
                else:
                    r = random.randint(0, 10 - size)
                    c = random.randint(0, 9)
                    coords = [(r + i, c) for i in range(size)]

                # Collision check
                if not any(local_grid[cr][cc] != " " for cr, cc in coords):
                    for cr, cc in coords:
                        local_grid[cr][cc] = letter
                    coord_labels = [f"{chr(ord('A') + cc)}{cr + 1}" for cr, cc in coords]
                    placed_ships.append({
                        "name": name,
                        "letter": letter,
                        "size": size,
                        "coords": [[cr, cc] for cr, cc in coords],
                        "coord_labels": coord_labels
                    })
                    break

        print("\n" + "=" * 55)
        print("          RANDOM FLEET DEPLOYMENT GENERATED            ")
        print("=" * 55)
        for ship in placed_ships:
            print(f"  {ship['letter']}: {ship['name']} ({ship['size']} slots) -> {', '.join(ship['coord_labels'])}")
        print("\n--- Your Deployed Defense Board ---")
        print(format_grid(local_grid, colorize=True))
        print("=" * 55)
        return placed_ships

    def _place_ships_manually(self) -> Optional[List[dict]]:
        """
        Prompts the player to position each of the 5 fleet ships in order:
          1. Aircraft Carrier (A) - 5 slots
          2. Battleship (B) - 4 slots
          3. Cruiser (C) - 3 slots
          4. Submarine (S) - 3 slots
          5. Destroyer (D) - 2 slots
        Validates boundaries and prevents overlapping placements.
        Returns the list of placed ship dictionaries, or None if user quits.
        """
        local_defense_grid = [[" " for _ in range(10)] for _ in range(10)]
        placed_ships = []

        print("\n" + "=" * 55)
        print("               MANUAL FLEET DEPLOYMENT (5 SHIPS)         ")
        print("=" * 55)
        print("Ships to position in order:")
        print("  1. Aircraft Carrier (A) - 5 slots")
        print("  2. Battleship       (B) - 4 slots")
        print("  3. Cruiser          (C) - 3 slots")
        print("  4. Submarine        (S) - 3 slots")
        print("  5. Destroyer        (D) - 2 slots")
        print("\nEnter starting coordinate (e.g. A1) and orientation")
        print("(H for horizontal, V for vertical). Example: 'A1 H' or 'B3 V'.")
        print("=" * 55)

        print("\n--- Initial Defense Board ---")
        print(format_grid(local_defense_grid, colorize=True))

        for idx, spec in enumerate(FLEET_SPEC):
            name = spec["name"]
            letter = spec["letter"]
            size = spec["size"]

            while True:
                print(f"\n[Ship {idx + 1}/5] {name} ({letter}) - Length: {size} slots")
                try:
                    user_input = input(f"Enter placement for {name} (e.g. 'A1 H' or 'B3 V') or 'quit': ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    self.disconnect()
                    return None

                if not user_input:
                    continue
                if user_input.lower() in ("quit", "exit", "q"):
                    self.disconnect()
                    return None

                parsed = parse_placement_input(user_input)
                if not parsed:
                    print("Error: Invalid input format. Use Column A-J and Row 1-10 (e.g. 'A1 H' or 'B3 V').")
                    continue

                col_letter, row_num, orient = parsed
                if not orient:
                    try:
                        orient_input = input(f"  Enter orientation for {name} (H for horizontal, V for vertical): ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        self.disconnect()
                        return None
                    if orient_input.lower() in ("quit", "exit", "q"):
                        self.disconnect()
                        return None
                    if not orient_input or orient_input[0].upper() not in ("H", "V"):
                        print("Error: Invalid orientation. Must be 'H' (horizontal) or 'V' (vertical).")
                        continue
                    orient = orient_input[0].upper()

                col_idx = ord(col_letter) - ord("A")
                row_idx = row_num - 1

                # 1. Bounds Validation
                if orient == "H":
                    if col_idx + size > 10:
                        print(f"Error: {name} ({letter}) placed horizontally from {col_letter}{row_num} extends beyond Column J (board boundary).")
                        print("Please re-enter coordinates.")
                        continue
                    coords = [(row_idx, col_idx + i) for i in range(size)]
                else:
                    if row_idx + size > 10:
                        print(f"Error: {name} ({letter}) placed vertically from {col_letter}{row_num} extends beyond Row 10 (board boundary).")
                        print("Please re-enter coordinates.")
                        continue
                    coords = [(row_idx + i, col_idx) for i in range(size)]

                # 2. Overlap Validation
                overlap_found = False
                for r, c in coords:
                    if local_defense_grid[r][c] != " ":
                        existing_letter = local_defense_grid[r][c]
                        existing_coord = f"{chr(ord('A') + c)}{r + 1}"
                        print(f"Error: Placement overlaps with existing ship '{existing_letter}' at {existing_coord}!")
                        print("Please re-enter coordinates.")
                        overlap_found = True
                        break

                if overlap_found:
                    continue

                # 3. Valid Placement - update board and record ship
                for r, c in coords:
                    local_defense_grid[r][c] = letter

                coord_labels = [f"{chr(ord('A') + c)}{r + 1}" for r, c in coords]
                placed_ships.append({
                    "name": name,
                    "letter": letter,
                    "size": size,
                    "coords": [[r, c] for r, c in coords],
                    "coord_labels": coord_labels
                })

                print(f"[+] Placed {name} ({letter}) at: {', '.join(coord_labels)}")
                print("\n--- Current Defense Board ---")
                print(format_grid(local_defense_grid, colorize=True))
                break

        print("\n" + "=" * 55)
        print("          ALL 5 SHIPS DEPLOYED SUCCESSFULLY!             ")
        print("=" * 55)
        return placed_ships

    def run(self):
        """
        Main execution loop. Coordinates interactive ship placement phase
        and player attack turns.
        """
        try:
            while self.running:
                # 1. Ship Placement Phase
                if self.state == ClientState.SHIP_PLACEMENT and self.placement_event.is_set():
                    self.placement_event.clear()
                    placed = self._deploy_fleet()
                    if placed:
                        self.state = ClientState.WAITING_FOR_TURN
                        print("[+] Transmitting fleet coordinates to server...")
                        if not self.send_message("PLACE_SHIPS", {"ships": placed}):
                            print("[Error] Failed to send ship placement to server.")
                            break
                        print("[*] Waiting for opponent to position their fleet...")
                    else:
                        break

                # 2. Player Turn Phase
                elif self.state == ClientState.PLAYER_TURN and self.my_turn_event.is_set():
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

                else:
                    time.sleep(0.1)

        except KeyboardInterrupt:
            print("\n[*] Interrupted by user.")
        finally:
            self.disconnect()

    def disconnect(self):
        """Performs graceful connection teardown and cleanup according to FSM."""
        if self.state != ClientState.DISCONNECTED and self.state != ClientState.CLEANUP:
            self.state = ClientState.CLEANUP
            self.running = False
            self.placement_event.set()
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
