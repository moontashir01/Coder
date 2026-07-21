# Skill Name: Debugging

## Description
A systematic method for finding and fixing bugs: reproduce, isolate,
hypothesize, make the smallest fix, and verify.

## Trigger Keywords
bug, debug, error, exception, traceback, crash, fix, broken, not working, fails,
failing, wrong output, unexpected, stack trace, issue, why is, doesn't work

## Instructions
When fixing a bug:
1. Reproduce it first — find the exact input or steps that trigger it before
   changing any code.
2. Read the actual error/traceback and locate the precise line. Do not guess.
3. Isolate the cause — narrow to the smallest code path that fails; add a
   temporary print/log or a failing test if it helps.
4. Form one hypothesis and make the SMALLEST change that fixes the root cause,
   not the symptom.
5. Verify by re-running the reproduction, and confirm nothing else broke.
6. Briefly explain what was wrong and why the fix works.
