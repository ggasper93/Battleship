### Sprint 1 AI Prompts
No Ai was used in sprint 1.

### Sprint 2 AI Prompts
No Ai was used in sprint 2.

### Sprint 3 AI Prompts
Initial Prompt for creating the client.py file:

We are creating a Battleship game that will be played over a network. The game will have a server and multiple clients. The server will manage the game state, while the clients will allow players to interact with the game.

We will start with the client.py file. This file will handle the client-side logic of the game, including connecting to the server, sending and receiving messages, and managing the player's game state.

Make use of the concurrency_design.md, protocol_blueprint.md, and fsm_specification.md files to understand the architecture, message protocol, and finite state machine of the game.

Key Constraints:
CLI Argument Parsing: The client.py file should accept command-line arguments for the server's IP address and port number. Use the argparse library to handle these arguments.
-h: displays help instructions
-i Server DNS hostname(required; resolved via socket.gethostbyname())
-p Server port(required)

Graceful DNS Resolution: The client should resolve the server's DNS hostname to an IP address using socket.gethostbyname(). If the resolution fails, the client must print an error message and exit gracefully. Rule: Catch DNS resolution errors explicitly. Forbidden: Allowing the client to crach with an unhandled exception or raw traceback printed to the console. Output: Output a clear, user friendly error message to check their router DNS configuration and exit cleanly.

AI Prompt for server.py file:
Moving on the the server.py file, we will implement the server-side logic of the Battleship game. Use the same concurrency_design.md, protocol_blueprint.md, and fsm_specification.md files to understand the architecture, message protocol, and finite state machine of the game.
Key Constraints:
CLI Argument Parsing: The server.py file should accept command-line arguments for the port number to listen on. Use the argparse library to handle these arguments.
-h: displays help instructions
-p Port number to listen on(required; binds to 0.0.0.0)

AI Prompt for adding ship placement selection and adjusting to each ship corresponding to a letter:
Initial generation looks good, one thing that needs adjustment right off the bat is that I want the players to position the ships themselves. I'd also like the different ships to be designated by different letters
A: Aircraft Carrier(5 slots)
B: Battleship(4 slots)
C: Cruiser(3 slots)
S: Submarine(3 slots)
D: Destroyer(2 slots)

Have them place the ships in order, and validate that the placement is valid (i.e., ships do not overlap and are within the bounds of the board). The player should be prompted to enter the starting coordinate and orientation (horizontal or vertical) for each ship. If the placement is invalid, prompt the player to re-enter the coordinates until a valid placement is made.

Ai Prompt Adjustments:
Lets give players the option to have the ships be randomly placed or place it themselves.

I'd also like the move result to be displayed below the last board update so it is easier to understand what happened. I'd also like to add colors to the board, specifically hits I want to be Red