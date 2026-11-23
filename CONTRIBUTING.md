# Contributing

Thanks for taking the time. This project values small, verifiable changes over
large speculative ones.

## Development setup

    git clone https://github.com/kele-tao/logscope
    cd logscope
    python -m venv .venv
    # Windows: .venv\Scripts\activate    POSIX: source .venv/bin/activate
    pip install -e ".[dev]"

## Before you open a pull request

    ruff check .        # style, import order, bugbears
    pytest -q           # the whole suite must stay green
    python -m logscope analyze examples/app.log

## Ground rules

- No new dependencies without a paragraph in the pull request explaining why the
  standard library cannot do the job. The zero-dependency promise is a feature,
  not an accident.
- Every behaviour gets a test. Tests must be offline and deterministic: no
  network, no sleeps, no reliance on the current time.
- Numbers in the README come from benchmarks/run.py. If you change the pipeline in
  a way that moves them, rerun the script and commit its output.
- Do not fabricate. If a feature is incomplete, say so in docs/ROADMAP.md rather
  than describing it as done.
- Keep public functions type-hinted and lines within 100 columns.

## Commit messages

Short imperative subject line, then a body explaining the why if it is not
obvious. Reference issues with #123.

## Reporting bugs

Use the issue templates. The most useful bug report includes the command you ran,
the first ~20 lines of the log (redacted) and the actual output.
