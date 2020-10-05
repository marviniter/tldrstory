"""
Indexing module
"""

import logging
import re
import sys
import time

from datetime import datetime

import praw
import yaml

from croniter import croniter
from txtai.embeddings import Embeddings
from txtai.labels import Labels

from .sqlite import SQLite

class Index(object):
    """
    Methods to build a new embeddings index.
    """

    @staticmethod
    def baseurl(url):
        """
        Extracts a base unique url for the input url. Used to help with url duplicate detection.

        Args:
            url: input url

        Returns:
            base url
        """

        # Remove parameters
        url = url.split("?", 1)[0]

        # Remove leading http(s?)://www
        url = re.sub(r"^http(s)?:\/\/(www.)?", "", url)

        # Remove trailing index.html
        url = re.sub(r"(index.htm(l)?)$", "", url)

        # Remove trailing slashes
        url = re.sub(r"\/?$", "", url)

        return url

    @staticmethod
    def accept(database, submission, ignore):
        """
        Filters a submission based on a series of rules.

        Args:
            database: database connection
            submission: submission entry
            ignore: list of domains to ignore

        Returns:
            True if accepted, False otherwise
        """

        # Get base url
        baseurl = Index.baseurl(submission.url)

        # Check that article doesn't already exist
        database.cur.execute("SELECT 1 FROM articles WHERE Id=? OR Reference LIKE ?", [submission.id, "%" + baseurl + "%"])

        # Accept submission if:
        #  - Submission id or url doesn't already exist
        #  - Submission is an external link
        #  - Submission link isn't an ignored pattern
        return not database.cur.fetchone() and not submission.is_self and submission.url.startswith("http") and \
               all([not re.search(pattern, submission.url) for pattern in ignore])

    @staticmethod
    def embeddings(index, database):
        """
        Builds an embeddings index.

        Args:
            index: index configuration
            database: database handle with content to index
        """

        # Create embeddings model, backed by sentence-transformers & transformers
        embeddings = Embeddings(index["embeddings"])

        database.execute("SELECT Id, Title FROM articles")

        # Create an index for the list of articles
        articles = [(uid, text, None) for uid, text in database.cur.fetchall()]
        embeddings.index(articles)

        logging.info("Built embedding index over %d stored articles", len(articles))

        # Save index
        embeddings.save(index["path"])

    @staticmethod
    def execute(index):
        """
        Executes an index run.

        Args:
            index: index configuration
        """

        logging.info("Refreshing index: %s", index["name"])

        # Text classifier
        classifier = Labels()

        # Reddit API instance
        reddit = praw.Reddit()

        # Output database
        database = SQLite(index["path"])

        # Reddit API configuration
        api = index["api"]

        # Execute each query
        for query in api["queries"]:
            # Filter for safe links
            query += " self:0 nsfw:0"

            for submission in reddit.subreddit(api["subreddit"]).search(query, sort=api["sort"], time_filter=api["time"], limit=None):
                # Parse create date
                date = datetime.fromtimestamp(submission.created_utc)

                # Only process recent external link posts
                if Index.accept(database, submission, api["ignore"]):
                    # Build article content and save
                    article = (submission.id, submission.subreddit.display_name.lower(), date, submission.title,
                               submission.url, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

                    # Build list of classification labels for text
                    labels = []
                    for name, config in index["labels"].items():
                        # Run classifier
                        result = classifier(submission.title, config["values"])

                        # Build list of labels for text
                        labels.extend([(None, submission.id, name) + x for x in result])

                    # Save article
                    database.save((article, labels))

        # Complete processing
        database.complete()

        # Build embeddings index
        Index.embeddings(index, database)

        # Close database
        database.close()

        logging.info("Indexing complete")

    @staticmethod
    def schedule(index):
        """
        Schedules index runs through a job scheduler.

        Args:
            index: index configuration
        """

        logging.info("Indexing scheduler enabled for %s using schedule %s", index["name"], index["schedule"])

        while True:
            # Schedule using localtime
            schedule = croniter(index["schedule"], datetime.now().astimezone()).get_next(datetime)
            logging.info("Next run scheduled for %s", schedule.isoformat())
            time.sleep(schedule.timestamp() - time.time())

            Index.execute(index)

    @staticmethod
    def run(index):
        """
        Runs an indexing process.

        Args:
            index: path to index configuration
        """

        # Initialize logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(module)-10s: %(message)s")

        # Load pipeline YAML file
        with open(index, "r") as f:
            # Read configuration
            index = yaml.safe_load(f)

        if "name" not in index or "api" not in index:
            logging.error("Index name and api fields are required")
            return

        # Check if indexing should be scheduled or run a single time
        if "schedule" in index:
            # Job scheduler
            Index.schedule(index)
        else:
            # Single run
            Index.execute(index)

if __name__ == "__main__":
    Index.run(sys.argv[1])
