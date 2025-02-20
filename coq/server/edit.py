from dataclasses import dataclass
from itertools import chain, repeat
from os import linesep
from pprint import pformat
from typing import (
    AbstractSet,
    Iterable,
    Iterator,
    MutableMapping,
    MutableSequence,
    Optional,
    Sequence,
    Tuple,
)
from uuid import uuid4

from pynvim import Nvim
from pynvim.api import Buffer, Window
from pynvim.api.common import NvimError
from pynvim_pp.api import (
    buf_get_extmarks,
    buf_get_lines,
    buf_set_text,
    create_ns,
    cur_win,
    win_get_buf,
    win_get_cursor,
    win_set_cursor,
)
from pynvim_pp.lib import write
from pynvim_pp.logging import log
from std2.types import never

from ..consts import DEBUG
from ..lang import LANG
from ..shared.trans import trans_adjusted
from ..shared.types import (
    UTF8,
    UTF16,
    ApplicableEdit,
    Context,
    ContextualEdit,
    Edit,
    Mark,
    NvimPos,
    RangeEdit,
    SnippetEdit,
)
from ..snippets.parse import ParsedEdit, parse
from ..snippets.parsers.types import ParseError
from .mark import mark
from .nvim.completions import UserData
from .rt_types import Stack
from .state import State

NS = uuid4()


@dataclass(frozen=True)
class EditInstruction:
    primary: bool
    begin: NvimPos
    end: NvimPos
    cursor_yoffset: int
    cursor_xpos: int
    new_lines: Sequence[str]


@dataclass(frozen=True)
class _Lines:
    lines: Sequence[str]
    b_lines8: Sequence[bytes]
    b_lines16: Sequence[bytes]
    len8: Sequence[int]


def _lines(lines: Sequence[str]) -> _Lines:
    b_lines8 = tuple(line.encode(UTF8) for line in lines)
    return _Lines(
        lines=lines,
        b_lines8=b_lines8,
        b_lines16=tuple(line.encode(UTF16) for line in lines),
        len8=tuple(len(line) for line in b_lines8),
    )


def _rows_to_fetch(
    ctx: Context,
    edit: ApplicableEdit,
    *edits: ApplicableEdit,
) -> Tuple[int, int]:
    row, _ = ctx.position

    def cont() -> Iterator[int]:
        for e in chain((edit,), edits):
            if isinstance(e, ContextualEdit):
                lo = row - (len(e.old_prefix.split(ctx.linefeed)) - 1)
                hi = row + (len(e.old_suffix.split(ctx.linefeed)) - 1)
                yield from (lo, hi)

            elif isinstance(e, RangeEdit):
                (lo, _), (hi, _) = e.begin, e.end
                yield from (lo, hi)

            elif isinstance(e, Edit):
                yield row

            else:
                never(e)

    line_nums = tuple(cont())
    return min(line_nums), max(line_nums) + 1


def _contextual_edit_trans(
    ctx: Context, lines: _Lines, edit: ContextualEdit
) -> EditInstruction:
    row, col = ctx.position
    old_prefix_lines = edit.old_prefix.split(ctx.linefeed)
    old_suffix_lines = edit.old_suffix.split(ctx.linefeed)

    r1 = row - (len(old_prefix_lines) - 1)
    r2 = row + (len(old_suffix_lines) - 1)

    c1 = (
        lines.len8[r1] - len(old_prefix_lines[0].encode(UTF8))
        if len(old_prefix_lines) > 1
        else col - len(old_prefix_lines[0].encode(UTF8))
    )
    c2 = (
        len(old_suffix_lines[-1].encode(UTF8))
        if len(old_prefix_lines) > 1
        else col + len(old_suffix_lines[0].encode(UTF8))
    )

    begin = r1, c1
    end = r2, c2

    new_lines = edit.new_text.split(ctx.linefeed)
    new_prefix_lines = edit.new_prefix.split(ctx.linefeed)
    cursor_yoffset = -len(old_prefix_lines) + len(new_prefix_lines)
    cursor_xpos = (
        len(new_prefix_lines[-1].encode(UTF8))
        if len(new_prefix_lines) > 1
        else len(ctx.line_before.encode(UTF8))
        - len(old_prefix_lines[-1].encode(UTF8))
        + len(new_prefix_lines[0].encode(UTF8))
    )

    inst = EditInstruction(
        primary=True,
        begin=begin,
        end=end,
        cursor_yoffset=cursor_yoffset,
        cursor_xpos=cursor_xpos,
        new_lines=new_lines,
    )
    return inst


def _edit_trans(
    unifying_chars: AbstractSet[str],
    ctx: Context,
    lines: _Lines,
    edit: Edit,
) -> EditInstruction:

    adjusted = trans_adjusted(unifying_chars, ctx=ctx, edit=edit)
    inst = _contextual_edit_trans(ctx, lines=lines, edit=adjusted)
    return inst


def _range_edit_trans(
    unifying_chars: AbstractSet[str],
    ctx: Context,
    primary: bool,
    lines: _Lines,
    edit: RangeEdit,
) -> EditInstruction:
    new_lines = edit.new_text.split(ctx.linefeed)

    if (
        primary
        and not isinstance(edit, ParsedEdit)
        and len(new_lines) <= 1
        and edit.begin == edit.end
    ):
        return _edit_trans(unifying_chars, ctx=ctx, lines=lines, edit=edit)

    else:
        (r1, ec1), (r2, ec2) = sorted((edit.begin, edit.end))

        if edit.encoding == UTF16:
            c1 = len(lines.b_lines16[r1][: ec1 * 2].decode(UTF16).encode(UTF8))
            c2 = len(lines.b_lines16[r2][: ec2 * 2].decode(UTF16).encode(UTF8))
        elif edit.encoding == UTF8:
            c1 = len(lines.b_lines8[r1][:ec1])
            c2 = len(lines.b_lines8[r2][:ec2])
        else:
            raise ValueError(f"Unknown encoding -- {edit.encoding}")

        begin = r1, c1
        end = r2, c2

        lines_before = (
            edit.new_prefix.split(ctx.linefeed)
            if isinstance(edit, ParsedEdit)
            else new_lines
        )
        cursor_yoffset = (r2 - r1) + (len(lines_before) - 1)
        cursor_xpos = (
            (
                len(lines_before[-1].encode(UTF8))
                if len(lines_before) > 1
                else len(lines.b_lines8[r2][:c1]) + len(lines_before[0].encode(UTF8))
            )
            if primary
            else -1
        )

        inst = EditInstruction(
            primary=primary,
            begin=begin,
            end=end,
            cursor_yoffset=cursor_yoffset,
            cursor_xpos=cursor_xpos,
            new_lines=new_lines,
        )
        return inst


def _instructions(
    ctx: Context,
    unifying_chars: AbstractSet[str],
    lines: _Lines,
    primary: ApplicableEdit,
    secondary: Sequence[RangeEdit],
) -> Iterator[EditInstruction]:
    if isinstance(primary, RangeEdit):
        inst = _range_edit_trans(
            unifying_chars,
            ctx=ctx,
            primary=True,
            lines=lines,
            edit=primary,
        )
        yield inst

    elif isinstance(primary, ContextualEdit):
        inst = _contextual_edit_trans(ctx, lines=lines, edit=primary)
        yield inst

    elif isinstance(primary, Edit):
        inst = _edit_trans(unifying_chars, ctx=ctx, lines=lines, edit=primary)
        yield inst

    else:
        never(primary)

    for edit in secondary:
        yield _range_edit_trans(
            unifying_chars,
            ctx=ctx,
            primary=False,
            lines=lines,
            edit=edit,
        )


def _consolidate(
    instruction: EditInstruction, *instructions: EditInstruction
) -> Sequence[EditInstruction]:
    edits = sorted(chain((instruction,), instructions), key=lambda i: (i.begin, i.end))
    pivot = 0, 0
    stack: MutableSequence[EditInstruction] = []

    for edit in edits:
        if edit.begin >= pivot:
            stack.append(edit)
            pivot = edit.end

        elif edit.primary:
            while stack:
                conflicting = stack.pop()
                if conflicting.end <= edit.begin:
                    break
            stack.append(edit)
            pivot = edit.end

        else:
            pass

    return stack


def _shift(instructions: Iterable[EditInstruction]) -> Iterator[EditInstruction]:
    row_shift = 0
    col_shift: MutableMapping[int, int] = {}

    for inst in instructions:
        (r1, c1), (r2, c2) = inst.begin, inst.end
        yield EditInstruction(
            primary=inst.primary,
            begin=(r1 + row_shift, c1 + col_shift.get(r1, 0)),
            end=(r2 + row_shift, c2 + col_shift.get(r2, 0)),
            cursor_yoffset=inst.cursor_yoffset,
            cursor_xpos=inst.cursor_xpos,
            new_lines=inst.new_lines,
        )
        row_shift += (r2 - r1) + len(inst.new_lines) - 1
        f_length = len(inst.new_lines[-1].encode(UTF8)) if inst.new_lines else 0
        col_shift[r2] = -(c2 - c1) + f_length if r1 == r2 else -c2 + f_length


def apply(nvim: Nvim, buf: Buffer, instructions: Iterable[EditInstruction]) -> None:
    for inst in _shift(instructions):
        try:
            buf_set_text(
                nvim, buf=buf, begin=inst.begin, end=inst.end, text=inst.new_lines
            )
        except NvimError as e:
            log.warn(f"%s{linesep}%s", e, inst)


def _cursor(cursor: NvimPos, instructions: Iterable[EditInstruction]) -> NvimPos:
    row, _ = cursor
    col = -1

    for inst in instructions:
        row += inst.cursor_yoffset
        col = inst.cursor_xpos
        if inst.primary:
            break

    assert col != -1
    return row, col


def _parse(stack: Stack, state: State, data: UserData) -> Tuple[Edit, Sequence[Mark]]:
    if isinstance(data.primary_edit, SnippetEdit):
        visual = ""
        return parse(
            stack.settings.match.unifying_chars,
            context=state.context,
            snippet=data.primary_edit,
            visual=visual,
        )
    else:
        return data.primary_edit, ()


def _restore(nvim: Nvim, win: Window, buf: Buffer, pos: NvimPos) -> Tuple[str, int]:
    row, _ = pos
    ns = create_ns(nvim, ns=NS)
    m1, m2 = buf_get_extmarks(nvim, buf=buf, id=ns)
    after, *_ = buf_get_lines(nvim, buf=buf, lo=row, hi=row + 1)
    cur_row, cur_col = win_get_cursor(nvim, win=win)

    (_, lo), (_, hi) = m1.end, m2.begin
    inserted = after.encode(UTF8)[lo:hi].decode(UTF8, errors="ignore")

    if inserted and cur_row == row and lo <= cur_col <= hi:
        movement = cur_col - lo
    else:
        movement = len(inserted.encode(UTF8))

    if inserted:
        buf_set_text(nvim, buf=buf, begin=m1.end, end=m2.begin, text=("",))

    return inserted, movement


def edit(
    nvim: Nvim, stack: Stack, state: State, data: UserData
) -> Optional[Tuple[int, int]]:
    win = cur_win(nvim)
    buf = win_get_buf(nvim, win=win)
    if buf.number != state.context.buf_id:
        log.warn("%s", "stale buffer")
        return None
    else:
        nvim.options["undolevels"] = nvim.options["undolevels"]
        inserted, movement = _restore(
            nvim, win=win, buf=buf, pos=state.context.position
        )

        try:
            primary, marks = _parse(stack, state=state, data=data)
        except ParseError as e:
            primary, marks = data.primary_edit, ()
            write(nvim, LANG("failed to parse snippet"))
            log.info("%s", e)

        lo, hi = _rows_to_fetch(
            state.context,
            primary,
            *data.secondary_edits,
        )
        if lo < 0 or hi > state.context.line_count + 1:
            log.warn("%s", pformat(("OUT OF BOUNDS", (lo, hi), data)))
            return None
        else:
            limited_lines = buf_get_lines(nvim, buf=buf, lo=lo, hi=hi)
            lines = [*chain(repeat("", times=lo), limited_lines)]
            view = _lines(lines)

            instructions = _consolidate(
                *_instructions(
                    state.context,
                    unifying_chars=stack.settings.match.unifying_chars,
                    lines=view,
                    primary=primary,
                    secondary=data.secondary_edits,
                )
            )
            n_row, p_col = _cursor(
                state.context.position,
                instructions=instructions,
            )
            n_col = p_col + movement

            stack.idb.inserted(data.instance.bytes, sort_by=data.sort_by)

            apply(nvim, buf=buf, instructions=instructions)
            if inserted:
                try:
                    buf_set_text(
                        nvim,
                        buf=buf,
                        begin=(n_row, p_col),
                        end=(n_row, p_col),
                        text=(inserted,),
                    )
                except NvimError as e:
                    log.warn("%s", e)

            win_set_cursor(nvim, win=win, row=n_row, col=n_col)
            if marks:
                mark(nvim, settings=stack.settings, buf=buf, marks=marks)

            if DEBUG:
                log.debug(
                    "%s",
                    pformat(((data.primary_edit, *data.secondary_edits), instructions)),
                )
            return n_row, n_col
