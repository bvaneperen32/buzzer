# Import order matters — tables with FKs must come after the tables they reference.
from .user import User  # noqa: F401 (no FK deps)
from .course import Course, CourseRoster, UserCourseAccess  # noqa: F401
from .game import Game, Question, UserGameAccess  # noqa: F401
from .image import Image  # noqa: F401
from .session import GameSession, SessionScore  # noqa: F401
