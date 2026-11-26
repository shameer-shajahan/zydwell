"""Every skill the assistant has, by the intent name the model gives it."""
from .add_guest import AddGuest
from .check_in import CheckIn
from .deposit import Deposit
from .edit_guest import EditGuest
from .edit_stay import EditCheckIn
from .edit_terms import EditTerms
from .expense import Expense, Income
from .leaving import Leaving
from .payment import Payment
from .questions import Approvals, Beds, Documents, GuestInfo, Money, Movements, Overdue
from .reminder import Reminder
from .transfer import Transfer

#: Jobs first, then questions — the order "help" lists them in.
SKILLS = {skill.name: skill for skill in [
    AddGuest(), CheckIn(), Transfer(), EditCheckIn(), EditTerms(), EditGuest(), Payment(), Deposit(),
    Leaving('check_out'), Leaving('give_notice'), Expense(), Income(), Reminder(),
    GuestInfo(), Beds('free_beds'), Beds('occupancy'), Overdue(), Money(),
    Movements('leaving_list'), Movements('joining_list'), Documents(), Approvals(),
]}
