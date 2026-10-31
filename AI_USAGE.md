# AI Usage

## Tools

- ChatGPT: Used to review the task requirements and help implement the SQLAlchemy models, Alembic migration, and FastAPI endpoints.
- Local pytest: Used to test the implementation against the provided tests.

## What AI got wrong

- An early draft included an unused Text import. I checked the required schema and kept memo as a nullable String(140).
- I reviewed the implementation to ensure the idempotency record is saved in the same database transaction as the transfer.

## In my own words

1. A transfer to someone else's account returns 404 instead of 403 to avoid revealing whether that account exists or belongs to another customer. Returning 403 could reveal information about another customer's account.

2. If the idempotency record were saved in a separate transaction after the transfer, the transfer could succeed while saving the record fails. A retry might then create another transfer and charge the customer twice.