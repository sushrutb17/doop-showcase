---
name: doop-training
description: Answer the owner's today, pre-workout, and weekly-review questions using Doop's minimum-disclosure read-only relay tools.
---

# Doop training workflow

Use this skill whenever the owner asks what to do, mentions heading to the gym,
asks about recovery or recent performance, or requests a weekly review.

## Today

1. Resolve the current local calendar date from the configured timezone.
2. Call `get_telegram_training_guidance(date)`.
3. Return its `text` verbatim. Its completion and recovery state are already
   canonical and already includes the completion state, recovery confidence,
   recommendation, and safety override. Do not make a second call, summarize,
   reformat, reinterpret, or add a personality line.
   When applicable, that canonical text says “planned; not yet logged”; never
   substitute “confirmed,” “completed,” or “done.” The word “confirmed” is forbidden
   unless a separate confirmed-action result exists.

## Pre-workout

1. Call `get_telegram_training_guidance(date)` exactly once.
2. Return its `text` verbatim; it already contains today's exact prescription.
3. Do not append questions or unplanned work. The canonical relay already
   contains the safety override.

## History or progression

Detailed history/progression is not exposed in the Stage 1 Telegram surface.
Say that this view remains in the local Doop Training dashboard; do not infer
history from chat memory or substitute a different tool.

## Weekly review

1. Determine the Monday for the requested week.
2. Call `get_telegram_weekly_review(week_start)`.
3. Return its `text` verbatim, including the missing-log caveat.
4. Do not make a second tool call or add any conclusion.

## Read-only boundary

Do not claim to log, confirm, cancel, accept, or reject anything. In this stage,
direct the owner to the local Doop Training dashboard for confirmed actions.
