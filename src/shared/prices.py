"""ap-south-1 (Mumbai) unit prices used by the dashboard cost card.

SOURCE: AWS public pricing pages (Step Functions, Lambda, DynamoDB on-demand, S3 Standard) - the numbers
below are my best recollection and MUST be re-checked against the pages before quoting them publicly.
Free tiers are ignored on purpose: the card shows the marginal cost of ONE family.

Pricing pages (check ap-south-1 / Asia Pacific (Mumbai)):
  Step Functions  https://aws.amazon.com/step-functions/pricing/
  Lambda          https://aws.amazon.com/lambda/pricing/
  DynamoDB        https://aws.amazon.com/dynamodb/pricing/on-demand/
  S3              https://aws.amazon.com/s3/pricing/
"""
SFN_FREE_TRANSITIONS = 4000                  # Step Functions Standard free tier, per month
USD_PER_STATE_TRANSITION = 0.025 / 1000      # Step Functions Standard, per 1,000 transitions
USD_PER_LAMBDA_REQUEST = 0.20 / 1_000_000    # per 1M requests
USD_PER_GB_SECOND = 0.0000133334             # Lambda arm64
LAMBDA_GB = 0.25                             # 256 MB functions
LAMBDA_AVG_SECONDS = 0.3                     # typical duration of our handlers
USD_PER_DDB_WRITE = 1.4231 / 1_000_000       # on-demand write request unit
USD_PER_S3_PUT = 0.005 / 1000                # S3 Standard PUT
INR_PER_USD = 88.0                           # fixed demo rate
