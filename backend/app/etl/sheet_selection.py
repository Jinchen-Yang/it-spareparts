from dataclasses import dataclass
from typing import Generic, Protocol, Sequence, TypeVar

from app.etl import mapping


class SelectableSheet(Protocol):
    sheet_name: str
    file_type: str | None


SheetT = TypeVar("SheetT", bound=SelectableSheet)

SELECTED = "selected"
IGNORED_RECOGNIZED = "ignored_recognized"
IGNORED_UNRECOGNIZED = "ignored_unrecognized"


@dataclass(frozen=True)
class WorkbookSelection(Generic[SheetT]):
    file_type: str | None
    selected: tuple[SheetT, ...]
    ignored_recognized: tuple[SheetT, ...]
    ignored_unrecognized: tuple[SheetT, ...]

    def action_for(self, sheet: SheetT) -> str:
        selected_names = {item.sheet_name for item in self.selected}
        ignored_names = {item.sheet_name for item in self.ignored_recognized}
        if sheet.sheet_name in selected_names:
            return SELECTED
        if sheet.sheet_name in ignored_names:
            return IGNORED_RECOGNIZED
        return IGNORED_UNRECOGNIZED


def select_workbook_sheets(sheets: Sequence[SheetT]) -> WorkbookSelection[SheetT]:
    recognized = tuple(sheet for sheet in sheets if sheet.file_type is not None)
    unrecognized = tuple(sheet for sheet in sheets if sheet.file_type is None)
    expense_sheets = tuple(
        sheet for sheet in recognized if sheet.file_type == mapping.EXPENSE
    )

    if expense_sheets:
        ignored = tuple(sheet for sheet in recognized if sheet.file_type != mapping.EXPENSE)
        file_type = "workbook" if len(recognized) > 1 else mapping.EXPENSE
        return WorkbookSelection(file_type, expense_sheets, ignored, unrecognized)

    if recognized:
        return WorkbookSelection(
            recognized[0].file_type,
            recognized[:1],
            recognized[1:],
            unrecognized,
        )

    return WorkbookSelection(None, (), (), unrecognized)
