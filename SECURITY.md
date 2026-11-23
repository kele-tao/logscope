# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x | yes |

## Reporting a vulnerability

Open a private security advisory at
https://github.com/kele-tao/logscope/security/advisories/new, or email the
maintainer listed on the GitHub profile. Please do not open a public issue for a
suspected vulnerability. You can expect an acknowledgement within five working
days.

## Threat model

logscope is an offline analysis tool. It reads log files you already have and
writes reports to paths you name. By design it:

- makes no network calls at runtime and loads no remote assets in the HTML report
  (inline SVG only, no CDN, no JavaScript);
- has zero third-party dependencies, so there is no supply chain to audit beyond
  the Python standard library;
- HTML-escapes every field that originates from the log before it reaches the
  report, so a malicious log line cannot inject markup or script;
- does not execute any part of the log and does not read files other than the
  ones you pass on the command line.

The realistic risks are therefore (a) pointing it at a log path you did not
intend, and (b) writing the report over an important file. Pass explicit paths
and use --out deliberately.
