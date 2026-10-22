import os
import sys

# The app's modules import each other by bare name (`from config import settings`), and config
# requires an API key to load - neither matters for the pure parsing/filtering logic under test.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("ONE_MIN_API_KEY", "test")
