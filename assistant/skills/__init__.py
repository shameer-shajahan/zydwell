"""Every skill the assistant has, by the intent name the model gives it."""
from .add_guest import AddGuest
from .check_in import CheckIn
from .edit_stay import EditCheckIn
from .expense import Expense
from .leaving import Leaving
from .payment import Payment
from .questions import Beds, GuestInfo, Money, Overdue

#: Jobs first, then questions — the order "help" lists them in.
SKILLS = {skill.name: skill for skill in [
    AddGuest(), CheckIn(), EditCheckIn(), Payment(), Leaving('check_out'),
    Leaving('give_notice'), Expense(),
    GuestInfo(), Beds('free_beds'), Beds('occupancy'), Overdue(), Money(),
]}
