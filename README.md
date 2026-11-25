# Cripto Kraken Reddit integration

This repository contains the read-only Python collector submitted for Reddit API review. The intended user runs a private paper-trading dashboard and wants to test whether public crypto discussions provide useful context before simulated decisions. Live collection requires approved Reddit access and OAuth credentials supplied by the host application.

## Proposed use

Every 15 minutes the host would check a combined new-post listing for r/Bitcoin, r/ethereum and r/CryptoCurrency. The collector can paginate for missed posts and check previously seen IDs for removals. It handles OAuth token expiry and Reddit rate-limit responses.

The collector returns each post's ID, subreddit, title, permalink, publication time, first observation time and normalized external article URL when present. Posts linking to the same article can be grouped while keeping the individual discussion links. The host manages local storage and expiry. The collector itself returns records in memory.

The application seeks permission to send post titles to the OpenAI API for sentiment classification and display the results in a private dashboard. This repository covers collection only. Classification and possible future use for live trading are outside this code sample.

## Review the code

Read `reddit_collector.py` for the OAuth exchange, API requests, removal checks and fields retained. This version extracts the collector and its URL helpers into a standalone module and uses Python's default TLS trust settings. Run the offline tests with Python 3.10 or newer:

```sh
python3 -m unittest -v
```

The tests use synthetic posts and require no account or credentials.
