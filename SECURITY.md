# Security

SlideTwin is a research prototype: not a medical device, not for clinical use.

## Reporting a vulnerability

Please report it privately, with "Report a vulnerability" in this repository's Security tab, not
in a public issue. Say what you found, how to reproduce it and what it allows.

## What SlideTwin protects against

- Slides from untrusted sources. The reader is chosen from the file's content, structures are
  checked and sizes bounded before a library parses them, and the libraries that crash on damaged
  files (slideio, libisyntax) run in worker processes: a bad file fails one read, not the server.
- Leaks of ground truth or file names. Labels and annotations never reach a session; errors and
  logs name slide ids, not files; label and macro images are never read.
- Requests from other sites. The server answers only allowed host names and browser origins
  (DNS rebinding), bounds request bodies, and in the Docker image limits requests per client.

## What it does not do

- Authentication: anyone who can reach the server can read its slides. Serve internal slides on
  localhost, or behind your own access control.
- Sandboxing of reader plugins: a plugin runs with the server's rights.
- Proof of authenticity: a replay shows that a log is reproducible, not who wrote it.
