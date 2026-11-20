# Volleyball Discord Bot

## Commands

- `!polls` lists the five most recent polls.
- `!poll friday` searches poll titles and shows tracked votes for a matching poll.
- `!poll 9/29` also matches polls posted on September 29.
- `!pollevent <poll message link>` creates an event from a poll with at least 12 votes, including votes cast before the bot was running. You can also use the poll message ID.
- `!reminder` posts today's or tomorrow's volleyball event in `#general`.

## Test event creation

Right-click a poll in the configured poll channel and choose **Copy Message Link**. In Discord, type `!pollevent` followed by the link.

Use a poll with at least 12 votes and a future start time. Its title should include a date, time range, and location, for example `🏐 Indoor 11/28/26 Bill McDonald 6–8 PM`. Both you and the bot need **Create Events** permission.

The bot also creates events automatically at 12 votes. New event announcements and reminders ping `@everyone` in `#general`. Automatic reminders go out about 24 and 10 hours before the event. The bot needs permission to mention `@everyone` in `#general`.
