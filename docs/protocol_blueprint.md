## Application-Layer Messaging Protocol Blueprint (Sprint 1 Deliverable)

### Message Transport & Serialization Format
- **Transport Protocol:** TCP
- **Serialization Format:** [JSON]
- **Framing Mechanism:** [Newline-delimited (`\n`) JSON payloads]
### Framing Example

{"msg_type" : "CONNECT", "player_id" : "Garret", "timestamp" : 00000000000}\n{"msg_type" : "DISCONNECT","player_id" : "Garret", "timestamp" : 00000000000}\n
### Message Schema Definitions

#### Message Types:
1. `CONNECT` (Client -> Server): Request to join the game room.
```json
{
  "msg_type" : "CONNECT",
  "player_id" : "Player",
  "timestamp" : 00000000000
}
```

2. `LOBBY_WAIT` (Server -> Client): Notification that server is waiting for Player 2.
```json
{
  "msg_type" : "LOBBY_WAIT",
  "player_id" : "Player",
  "timestamp" : 00000000000
}
```

3. `GAME_START` (Server -> Clients): Game initiated, assigns roles (e.g. Player X vs Player O).
```json
{
  "msg_type" : "GAME_START",
  "player_id" : "Player",
  "timestamp" : 00000000000
}
```

4. `MOVE` (Client -> Server): Player action (e.g., cell coordinates or answer choice).
```json
{
  "msg_type" : "CONNECT",
  "player_id" : "Player",
  "payload" : {
    "column" : "A",
    "row" : "1"
  },
  "timestamp" : 00000000000
}
```

5. `STATE_UPDATE` (Server -> Clients): Broadcast current game board / state and active player turn.
```json
{
  "msg_type" : "STATE_UPDATE",
  "player_id" : "Player",
  "payload" : {
    "move result" : "hit",
    "offense board" : "
    |00|A|B|C|D|E|F|G|H|I|J|
    -----------------------
    |01| | | | | | | | | | |
    ------------------------
    |02| | | | | | | | | | |
    ------------------------
    |03| | | | | | | | | | |
    ------------------------
    |04| | | | | | | | | | |
    ------------------------
    |05| | | | | | | | | | |
    ------------------------
    |06| | | | | | | | | | |
    ------------------------
    |07| | | | | | | | | | |
    ------------------------
    |08| | | | | | | | | | |
    ------------------------
    |09| | | | | | | | | | |
    ------------------------
    |10| | | | | | | | | | |
    ------------------------
    ",

    "defense board" : "
    |00|A|B|C|D|E|F|G|H|I|J|
    -----------------------
    |01| | | | | | | | | | |
    ------------------------
    |02| | | | | | | | | | |
    ------------------------
    |03| | | | | | | | | | |
    ------------------------
    |04| | | | | | | | | | |
    ------------------------
    |05| | | | | | | | | | |
    ------------------------
    |06| | | | | | | | | | |
    ------------------------
    |07| | | | | | | | | | |
    ------------------------
    |08| | | | | | | | | | |
    ------------------------
    |09| | | | | | | | | | |
    ------------------------
    |10| | | | | | | | | | |
    ------------------------
    "

  },
  "timestamp" : 00000000000
}
```

6. `GAME_OVER` (Server -> Clients): Victory / Draw notification with final scores.
```json
{
  "msg_type" : "GAME_OVER",
  "player_id" : "Player",
  "payload" : {
    "winner" : "Player_1",
  },
  "timestamp" : 00000000000
}
```

7. `ERROR` (Server -> Client): Invalid move or malformed packet error.
```json
{
  "msg_type" : "ERROR",
  "player_id" : "Player",
  "payload" : {
    "error_code" : 400,
    "error_message" : "Invalid move: Cell already guessed."
  },
  "timestamp" : 00000000000
}
```

8. `DISCONNECT` (Client -> Server): Player voluntarily leaves the game.
```json
{
  "msg_type" : "DISCONNECT",
  "player_id" : "Player",
  "timestamp" : 00000000000
}
```

9. `RECONNECT PROMPT` (Server -> Client): Prompt for player to reconnect after a disconnection.
```json
{
  "msg_type" : "RECONNECT_PROMPT",
  "player_id" : "Player",
  "timestamp" : 00000000000
}
```

10. `RECONNECT` (Client -> Server): Player attempts to rejoin the game after a disconnection.
```json
{
  "msg_type" : "RECONNECT",
  "player_id" : "Player",
  "timestamp" : 00000000000
}
```