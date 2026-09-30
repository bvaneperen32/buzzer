"""Socket.io event name constants."""

# Client → Server
JOIN_ROOM = "join_room"
REJOIN_ROOM = "rejoin_room"
HOST_ADVANCE = "host_advance"
HOST_LOCK_QUESTION = "host_lock_question"
SUBMIT_ANSWER = "submit_answer"

# Server → Client (broadcast to all in room)
NEW_QUESTION = "new_question"
QUESTION_RESULTS = "question_results"
GAME_OVER = "game_over"
PLAYER_JOINED = "player_joined"
PLAYER_LEFT = "player_left"
HOST_DISCONNECTED = "host_disconnected"
HOST_RECONNECTED = "host_reconnected"  # to the room: a host announced as gone is back
GAME_ABANDONED = "game_abandoned"

# Server → specific client
ANSWER_RECEIVED = "answer_received"
# An answer refused because its question locked or moved on: a normal outcome, not an ERROR
# (ERROR stays for malformed requests and authorization/session failures).
ANSWER_REJECTED = "answer_rejected"
# To the host: Show Results published nothing, because an answer let in before the question
# closed is still being recorded. Also not an ERROR: the host can show the results again.
RESULTS_DELAYED = "results_delayed"
SYNC_STATE = "sync_state"
ERROR = "error"

# Server → Host only
ANSWER_STATUS = "answer_status"
ANSWER_PHASE_ENDED = "answer_phase_ended"

# Server → all clients in room
QUESTION_LOCKED = "question_locked"
QUESTION_UNLOCKED = "question_unlocked"
