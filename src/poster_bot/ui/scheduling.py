"""Durable scheduling cards; tokens carry no client-side scheduling data.

The flow owns confirmation and service transactions. These cards only collect
fields or offer fresh domain slots; they never reserve a minute themselves.
"""
from datetime import date, timedelta
from dataclasses import replace

from ..domain.errors import DomainError, PastTimeError, NonLocalTimeError, AmbiguousLocalTimeError
from ..domain.time import Instant, day_slots, local_to_instant, HORIZON_DAYS, recent_manual_suggestions
from .state import State
from .replies import Reply
from .times_input import local_day, parse_clock, parse_date
from . import texts

CARD_ACTIONS = frozenset({'hours_day', 'hour_pick', 'form_date', 'form_time',
                          'form_day', 'form_continue', 'form_back', 'form_card'})


class SchedulingCards:
    """Private OwnerFlow helper; shared dependencies remain owned by the flow."""

    def _confirmation_form(self, ui):
        """Recover editable fields without changing the confirmed service target."""
        local = Instant(ui.arg('minute_epoch')).local(self._settings.get().timezone)
        payload = dict(ui.payload)
        payload['purpose'] = ('schedule' if ui.state is State.CONFIRM_SCHEDULE
                              else ui.arg('purpose').removesuffix('_confirm'))
        payload['time_form'] = {'date': local.date().isoformat(),
                                'time': local.strftime('%H:%M'), 'field': 'time'}
        return replace(ui, payload=payload)

    def _schedule_candidates(self, now):
        """Twelve nearest configured free hours plus recent manual instants.

        One reservation snapshot, bounded by the domain's existing horizon.
        Explicit day views deliberately do not use this cross-day search.
        """
        settings = self._settings.get()
        today = local_day(now, settings.timezone)
        reserved = self._publications.reserved_minutes()
        candidates = {}
        for offset in range(HORIZON_DAYS):
            for slot in day_slots(today + timedelta(days=offset), settings.timezone,
                                  settings.start_hour, settings.end_hour):
                if slot.epoch > now and slot.minute not in reserved:
                    candidates[slot.epoch] = (slot, None)
                    if len(candidates) >= 12:
                        break
            if len(candidates) >= 12:
                break
        for suggestion in recent_manual_suggestions(
                self._manual_times.recent(limit=3), now, settings.timezone, limit=3):
            if suggestion.instant.minute not in reserved:
                candidates.setdefault(suggestion.instant.epoch,
                                      (suggestion.instant, (suggestion.hh, suggestion.mm)))
        return [candidates[key] for key in sorted(candidates)]

    def _card_btn(self, label, kind, *, now, **kwargs):
        return self._btn(label, kind, now=now,
                         expected_version=self._ui().version, **kwargs)

    def _free_hours(self, day, now):
        settings = self._settings.get()
        reserved = self._publications.reserved_minutes()
        return [slot for slot in day_slots(day, settings.timezone,
                                          settings.start_hour, settings.end_hour)
                if slot.epoch > now and slot.minute not in reserved]

    def _hours_view(self, *, now, post_id, day=None, page=0, note='', persist=True):
        settings = self._settings.get()
        today = local_day(now, settings.timezone)
        day = max(today, min(day or today, today + timedelta(days=HORIZON_DAYS - 1)))
        slots = self._free_hours(day, now)
        # Four chips per row, four rows per page; leaves room for navigation.
        pages = max(1, (len(slots) + 15) // 16)
        page = max(0, min(page, pages - 1))
        if persist:
            self._go(State.DRAFT, {'post_id': post_id,
                                  'hours': {'day': day.isoformat(), 'page': page}}, now=now)
        buttons = [self._card_btn(slot.local(settings.timezone).strftime('%H:%M'),
                                  'hour_pick', now=now, minute_epoch=slot.epoch)
                   for slot in slots[page * 16:(page + 1) * 16]]
        rows = [tuple(buttons[i:i + 4]) for i in range(0, len(buttons), 4)]
        if pages > 1:
            rows.append(tuple(self._card_btn(label, 'hours_day', now=now,
                                            arg=f'{day.isoformat()}:{target}')
                              for label, target in [('‹ Часы', page - 1), ('Часы ›', page + 1)]
                              if 0 <= target < pages))
        nav = []
        if day > today:
            nav.append(self._card_btn('← День', 'hours_day', now=now,
                                     arg=f'{(day - timedelta(days=1)).isoformat()}:0'))
        nav.append(self._card_btn('Сегодня', 'hours_day', now=now,
                                 arg=f'{today.isoformat()}:0'))
        if day < today + timedelta(days=HORIZON_DAYS - 1):
            nav.append(self._card_btn('День →', 'hours_day', now=now,
                                     arg=f'{(day + timedelta(days=1)).isoformat()}:0'))
        rows.append(tuple(nav))
        rows.append((self._card_btn('Назад', 'form_back', now=now),))
        text = f'Свободные часы — {day:%d.%m.%Y} ({settings.timezone}).'
        if not slots:
            text += '\nСвободных часов нет. Выберите следующий день.'
        if pages > 1:
            text += f'\nСтраница {page + 1}/{pages}.'
        return Reply(f'{note}\n{text}' if note else text, tuple(rows))

    def _start_time_form(self, *, now, payload):
        settings = self._settings.get()
        payload = {**payload, 'time_form': {
            'date': local_day(now, settings.timezone).isoformat(), 'time': None,
            'field': 'time'}}
        self._go(State.MANUAL_TIME, payload, now=now)
        return self._time_form_view(now=now)

    def _text_schedule_picker(self, *, now, ui, text, source_key):
        """Capture a bare clock directly, without a transient empty form write."""
        clock = parse_clock(text)
        if clock is None:
            return Reply(f'{texts.TIME_BAD_INPUT}\n{texts.PICK_TIME}',
                         self._schedule_picker(now=now, post_id=ui.arg('post_id')).rows)
        post_id = ui.arg('post_id')
        # No await between inspecting the target and the atomic field/receipt
        # write. Publication services still own final status/version validation.
        self._drafts.get_content(post_id)
        if self._publications.get_by_post(post_id) is not None:
            return Reply(texts.ALREADY_PUB_RU)
        settings = self._settings.get()
        day = (date.fromisoformat(ui.arg('hours')['day']) if ui.arg('hours')
               else local_day(now, settings.timezone))
        form = {'date': day.isoformat(), 'time': f'{clock[0]:02d}:{clock[1]:02d}',
                'field': 'date'}
        payload = {key: value for key, value in ui.payload.items()
                   if key not in ('schedule_picker', 'hours')}
        payload.update(purpose='schedule', time_form=form)
        self._state.set(State.MANUAL_TIME, payload, expected_version=ui.version,
                        now_epoch=now, effect_key=source_key)
        return replace(self._form_continue(now=now), input_accepted=True)

    def _time_form_view(self, *, now, note=''):
        ui = self._ui()
        form = ui.arg('time_form')
        settings = self._settings.get()
        day = date.fromisoformat(form['date'])
        day_label = f'{day:%d.%m.%Y}'
        if day == local_day(now, settings.timezone):
            day_label = f'Сегодня ({day_label})'
        text = (f'Своё время ({settings.timezone})\nДата: {day_label}\n'
                f'Время: {form["time"] or "не выбрано"}')
        rows = [(self._card_btn('Изменить дату', 'form_date', now=now),),
                (self._card_btn('Назад', 'form_back', now=now),)]
        # Valid input normally advances immediately. A complete card can remain
        # after a preview failure; only then is an explicit retry useful.
        try:
            self._form_instant(ui=ui, now=now)
        except DomainError:
            pass
        else:
            rows.insert(0, (self._card_btn('Повторить предпросмотр',
                                         'form_continue', now=now),))
        if form['field'] == 'date':
            text += '\nПришлите дату ДД-ММ-ГГГГ, например 29-10-2026.'
            today = local_day(now, settings.timezone)
            rows.insert(0, tuple(self._card_btn(label, 'form_day', now=now,
                                               arg=(today + timedelta(days=offset)).isoformat())
                                 for label, offset in [('Сегодня', 0), ('Завтра', 1)]))
        elif form['field'] == 'time':
            text += '\nПришлите время для выбранной даты: 810, 8:10, 8-10, 8.10 или 8 10.'
        return Reply(f'{note}\n{text}' if note else text, tuple(rows))

    def _form_instant(self, *, ui, now):
        form = ui.arg('time_form')
        clock = parse_clock(form['time'] or '')
        if clock is None:
            raise DomainError('Выберите время ЧЧ:ММ.')
        instant = local_to_instant(date.fromisoformat(form['date']), *clock,
                                   self._settings.get().timezone)
        if instant.epoch <= now:
            raise PastTimeError(texts.TIME_NOT_FUTURE)
        reserved = self._publications.reserved_minutes()
        if ui.arg('pub_id') is not None:
            pub = self._publications.get(int(ui.arg('pub_id')))
            # The service owns status/version CAS; only its own minute may stay.
            reserved = reserved - {pub.minute_epoch}
        if instant.minute in reserved:
            raise DomainError(texts.MINUTE_TAKEN_RU)
        return instant, clock

    def _form_continue(self, *, now):
        ui = self._ui()
        try:
            instant, clock = self._form_instant(ui=ui, now=now)
        except (NonLocalTimeError, AmbiguousLocalTimeError):
            return self._time_form_view(now=now, note=texts.TIME_TZ_ISSUE)
        except DomainError as exc:
            return self._time_form_view(now=now, note=str(exc))
        if ui.arg('purpose') == 'schedule':
            return self._confirm_schedule_view(now=now, post_id=ui.arg('post_id'),
                                               instant=instant, manual=clock)
        return self._confirm_minute_change_view(now=now, ui=ui,
                                                instant=instant, manual=clock)

    def _card_action(self, *, now, action):
        ui = self._ui()
        kind = action.action
        if kind == 'hours_day':
            day, page = self._day_arg(action.arg)
            return self._hours_view(now=now, post_id=ui.arg('post_id'),
                                    day=date.fromisoformat(day), page=page)
        if kind == 'hour_pick':
            day = date.fromisoformat(ui.arg('hours')['day'])
            slots = self._free_hours(day, now)
            if action.minute_epoch not in {slot.epoch for slot in slots}:
                return self._hours_view(now=now, post_id=ui.arg('post_id'), day=day,
                                        note='Этот час уже недоступен. Выберите другой.')
            return self._confirm_schedule_view(now=now, post_id=ui.arg('post_id'),
                                               instant=Instant(action.minute_epoch))
        if kind == 'form_back':
            if ui.arg('pub_id') is not None:
                return self._publication_view(now=now, pub_id=ui.arg('pub_id'))
            return self._draft_view(now=now, post_id=ui.arg('post_id'))
        if kind == 'form_continue':
            return self._form_continue(now=now)
        if not ui.arg('time_form') and kind == 'form_date':
            ui = self._confirmation_form(ui)
        form = dict(ui.arg('time_form'))
        note = ''
        if kind == 'form_day':
            form.update(date=action.arg, field=None)
            note = self._validate_changed_date(form)
        else:
            form['field'] = {'form_date': 'date', 'form_time': 'time',
                             'form_card': None}[kind]
        self._go(State.MANUAL_TIME, {**ui.payload, 'time_form': form}, now=now)
        if kind == 'form_day' and form['time'] and not note:
            return self._form_continue(now=now)
        return self._time_form_view(now=now, note=note)

    def _validate_changed_date(self, form):
        """Keep the selected time except when it is not unique on the new day."""
        clock = parse_clock(form['time'] or '')
        if clock is not None:
            try:
                local_to_instant(date.fromisoformat(form['date']), *clock,
                                 self._settings.get().timezone)
            except (NonLocalTimeError, AmbiguousLocalTimeError):
                form.update(time=None, field='time')
                return texts.TIME_TZ_ISSUE + ' Введите другое время.'
        return ''

    async def _text_time_form(self, *, now, ui, text, source_key):
        form = dict(ui.arg('time_form'))
        note = ''
        def rejected(note):
            if not self._ui().arg('time_form'):
                reply = self._replay_reply(now=now)
                return replace(reply, text=f'{note}\n{reply.text}')
            return self._time_form_view(now=now, note=note)
        try:
            raw = text.strip()
            # Optional legacy combined shortcut; bare time always uses this date.
            if ' ' in raw and len(raw.split(' ', 1)[0]) == 10:
                date_raw, time_raw = raw.split(' ', 1)
                day = parse_date(date_raw)
                clock = parse_clock(time_raw)
                if clock is None:
                    raise ValueError
                form.update(date=day.isoformat(), time=f'{clock[0]:02d}:{clock[1]:02d}')
            elif form['field'] == 'date' and parse_clock(raw) is None:
                day = parse_date(raw)
                form['date'] = day.isoformat()
                note = self._validate_changed_date(form)
            else:
                clock = parse_clock(raw)
                if clock is None:
                    raise ValueError
                form['time'] = f'{clock[0]:02d}:{clock[1]:02d}'
            if date.fromisoformat(form['date']) < local_day(now, self._settings.get().timezone):
                return rejected(texts.TIME_NOT_FUTURE)
        except ValueError:
            note = texts.DATE_BAD_INPUT if form['field'] == 'date' else texts.TIME_BAD_INPUT
            return rejected(note)
        form['field'] = 'time'
        self._state.set(State.MANUAL_TIME, {**ui.payload, 'time_form': form},
                        expected_version=ui.version, now_epoch=now, effect_key=source_key)
        reply = (self._time_form_view(now=now, note=note) if note
                 else self._form_continue(now=now))
        return replace(reply, input_accepted=True)
