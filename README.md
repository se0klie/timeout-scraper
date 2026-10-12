# Timeout scraper

Two-step pipeline for studying how RPC timeouts are chosen in Apache server projects:

1. **Jira scraper** ([jira_scraper/apache_jira_scraper.py](jira_scraper/apache_jira_scraper.py)) downloads issues from the Apache Jira (`issues.apache.org`), optionally scoring them with keyword/regex criteria.
2. **Classifier agent** ([classifier_agent.py](classifier_agent.py)) sends each scraped issue to Gemini and classifies it as `TIMEOUT_CHANGE`, `TIMEOUT_STRATEGY`, `EXCLUDE` or `MANUAL_REVIEW`, with exact supporting evidence.

```
Apache Jira ──► apache_jira_scraper.py ──► <PROJECT>.csv ──► classifier_agent.py ──► classified_issues.csv
```

## Requirements

- Python 3.10+
- A Gemini API key (only for the classifier)

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r jira_scraper/requirements.txt   
python -m pip install -U google-genai                  
```

---

## 1. Run the Jira scraper

No credentials are needed; it uses the public Apache Jira REST API.

```bash
cd jira_scraper
```

Pick **one** of three query modes:

```bash
# Every issue in a project
python apache_jira_scraper.py --project HBASE --filter-criteria

# Issues in a project matching a full-text search
python apache_jira_scraper.py --project CASSANDRA --text "rpc timeout" --filter-criteria

# Search the project once per built-in RPC-timeout keyword
python apache_jira_scraper.py --rpc-timeouts HDFS --filter-criteria

# Any custom JQL
python apache_jira_scraper.py --jql 'project = KAFKA AND text ~ "request.timeout.ms"' --filter-criteria
```

`selection_score` is computed by the scraper's regex heuristics (RPC-timeout keywords, value changes, justification, performance trade-offs, experiments). The classifier uses it to process the most promising issues first.

---

## 2. Run the classifier agent

The agent reads a CSV produced by the scraper and calls Gemini for each issue.

```bash
export GEMINI_API_KEY='your-key'

python classifier_agent.py \
  --input jira_scraper/jira_output/HBASE/HBASE.csv \
  --output classified/HBASE_classified.csv \
  --model gemini-3.1-flash-lite-preview \
  --max-rows 100
```

The input CSV needs an issue ID column (`issue_ID`, `issue_id` or `key`). The agent sends these columns to the model when they exist: `project`, `project_key`, `project_name`, `title`, `summary`, `description`, `discussion`, `comments`, `comments_text`, `importance`.

- **Priority order:** if a score column exists, rows are processed from highest to lowest score.
