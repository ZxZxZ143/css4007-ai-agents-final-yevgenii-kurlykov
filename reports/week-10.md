# Semester Week 10 Progress Report

> **Revision note (November 25, 2026, semester Week 13).** This report was rewritten for the current project, DeskPilot. The facts about when the work was done are kept unchanged: no student commits were recorded during November 2–8.

## Completed Work

The Week 10 deliverable is the project proposal. The current [proposal](proposal.md) describes DeskPilot, a voice- and text-controlled autonomous desktop agent for Windows. Each action is checked in Python against file-system zones, risk classes, action-bound approval and execution limits. The proposal defines the problem, target users, Level A scope, functions and model tools, architecture, example requests, evaluation plan with sixteen tests including four guardrail tests, and milestones.

Timeline of the planning work. No student commits were recorded during the official window. Planning started late, on November 9, under an earlier topic and was revised on November 16 into AgentShield Lab. DeskPilot planning, including the [functional roadmap](../docs/ROADMAP.md) and [architecture](../docs/ARCHITECTURE.md), was done on November 25. Claude Code assisted with the DeskPilot planning documents.

## Important Commits

- `b537533`: inherited instructor starter, November 2.
- `9716249`: first student planning commit (earlier topic), November 9, after the Week 10 window.
- `4e7eea9`: AgentShield Lab plan, November 16.
- The DeskPilot planning revision is committed in Week 13 (November 25).

## Current Functionality

At the end of Week 10, the repository contained only the instructor utilities, example guardrails and report validators. No application code existed yet.

## Problems Encountered

The planning window closed before any student work was recorded. The topic was later changed twice. The final change came after a prototype of the previous topic already existed.

## Solutions

The original dates are recorded honestly, and each revision is labeled where it happened. The new plan orders tasks by functional dependency, so the core agent and its guardrails come before voice and UI. A text-only agent can then still meet Level A requirements if time runs short.

## What I Learned

Guardrails for an autonomous agent must be enforced in code at the moment of each action. A request filter alone cannot do this, because a harmless request can still lead to a harmful tool call, and malicious instructions can arrive later from a web page or file.

## Next Week

Week 11 was planned for initial LLM integration. The Week 11 report records the actual progress, which was made on the earlier topic.
