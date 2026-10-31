"""Rawaj Pay transfer API."""

import math
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.db import make_engine, make_session_factory
from app.models import Account, IdempotencyRecord, Transfer
from app.settings import Settings


def _problem(status: int, title: str, detail: str | None = None, headers=None):
    body = {
        "type": "about:blank",
        "title": title,
        "status": status,
    }

    if detail:
        body["detail"] = detail

    return JSONResponse(
        status_code=status,
        content=body,
        media_type="application/problem+json",
        headers=headers,
    )


def _transfer_json(transfer: Transfer) -> dict:
    return {
        "id": transfer.id,
        "from_account_id": transfer.from_account_id,
        "to_account_id": transfer.to_account_id,
        "amount_piastres": transfer.amount_piastres,
        "memo": transfer.memo,
        "status": transfer.status,
    }


def create_app(settings: Settings) -> FastAPI:

    app = FastAPI(title="Rawaj Pay Transfers")

    engine = make_engine(settings.database_url)

    app.state.engine = engine

    SessionLocal = make_session_factory(engine)

    # Rate limiter:
    # customer_id -> {"tokens": float, "updated_at": float}

    buckets: dict[str, dict[str, float]] = {}

    # ---------------------------------------
    # Authentication
    # ---------------------------------------

    def authenticate(request: Request):

        header = request.headers.get("Authorization", "")

        parts = header.split()

        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None

        return settings.api_tokens.get(parts[1])

    # ---------------------------------------
    # Rate Limiting
    # ---------------------------------------

    def consume_token(customer_id: str):

        now = settings.clock()

        capacity = settings.rate_limit_capacity

        refill = settings.rate_limit_refill_per_second

        bucket = buckets.get(customer_id)

        if bucket is None:

            tokens = float(capacity)

            updated_at = now

        else:

            elapsed = max(0.0, now - bucket["updated_at"])

            tokens = min(
                float(capacity),
                bucket["tokens"] + elapsed * refill,
            )

            updated_at = now

        if tokens < 1.0:

            buckets[customer_id] = {
                "tokens": tokens,
                "updated_at": updated_at,
            }

            seconds = (
                math.ceil((1.0 - tokens) / refill)
                if refill > 0
                else 1
            )

            return False, max(1, seconds)

        buckets[customer_id] = {
            "tokens": tokens - 1.0,
            "updated_at": updated_at,
        }

        return True, 0

    # ---------------------------------------
    # POST /v1/transfers
    # ---------------------------------------

    @app.post("/v1/transfers")
    async def create_transfer(request: Request):

        # 1. Authentication

        customer_id = authenticate(request)

        if customer_id is None:

            return _problem(
                401,
                "Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # 2. Rate limiting

        allowed, retry_after = consume_token(customer_id)

        if not allowed:

            return _problem(
                429,
                "Too Many Requests",
                headers={"Retry-After": str(retry_after)},
            )

        # 3. Idempotency key

        key = request.headers.get("Idempotency-Key")

        if key is None or key == "":

            return _problem(
                400,
                "Bad Request",
                "Idempotency-Key header is required.",
            )

        # 4. Read JSON body

        try:

            payload = await request.json()

        except Exception:

            return _problem(
                422,
                "Unprocessable Entity",
                "Request body must be valid JSON.",
            )

        if not isinstance(payload, dict):

            return _problem(
                422,
                "Unprocessable Entity",
                "Request body must be a JSON object.",
            )

        # 5. Validate fields

        allowed_fields = {
            "from_account_id",
            "to_account_id",
            "amount_piastres",
            "memo",
        }

        required_fields = {
            "from_account_id",
            "to_account_id",
            "amount_piastres",
        }

        if (
            set(payload) - allowed_fields
            or not required_fields.issubset(payload)
        ):

            return _problem(
                422,
                "Unprocessable Entity",
                "Invalid request fields.",
            )

        from_id = payload["from_account_id"]

        to_id = payload["to_account_id"]

        amount = payload["amount_piastres"]

        memo = payload.get("memo")

        if (
            not isinstance(from_id, str)
            or not isinstance(to_id, str)
            or type(amount) is not int
            or amount <= 0
            or amount > settings.max_transfer_piastres
            or (memo is not None and not isinstance(memo, str))
            or (isinstance(memo, str) and len(memo) > 140)
            or from_id == to_id
        ):

            return _problem(
                422,
                "Unprocessable Entity",
                "Invalid transfer details.",
            )

        # Normalize request body.
        # Missing memo and memo=null are treated equally.

        normalized_body = {
            "from_account_id": from_id,
            "to_account_id": to_id,
            "amount_piastres": amount,
            "memo": memo,
        }

        # 6. Database transaction

        with SessionLocal() as session:

            with session.begin():

                # Check previous idempotency record

                previous = session.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.customer_id == customer_id,
                        IdempotencyRecord.key == key,
                    )
                )

                if previous is not None:

                    # Same key, different body

                    if previous.request_body != normalized_body:

                        return _problem(
                            422,
                            "Unprocessable Entity",
                            "Idempotency-Key was already used with a different request.",
                        )

                    # Same key, same body:
                    # Replay the original response

                    return JSONResponse(
                        status_code=previous.response_status,
                        content=previous.response_body,
                        media_type=(
                            "application/problem+json"
                            if previous.response_status >= 400
                            else "application/json"
                        ),
                        headers={
                            "Idempotent-Replayed": "true"
                        },
                    )

                # 7. Check source account ownership

                source = session.get(Account, from_id)

                if source is None or source.owner_id != customer_id:

                    return _problem(
                        404,
                        "Not Found",
                        "Source account not found.",
                    )

                # 8. Check destination account

                destination = session.get(Account, to_id)

                if destination is None:

                    return _problem(
                        422,
                        "Unprocessable Entity",
                        "Destination account not found.",
                    )

                # 9. Check sufficient balance

                if source.balance_piastres < amount:

                    response_body = {
                        "type": "about:blank",
                        "title": "Conflict",
                        "status": 409,
                        "detail": "Insufficient funds.",
                    }

                    # Save 409 response for idempotency

                    session.add(
                        IdempotencyRecord(
                            customer_id=customer_id,
                            key=key,
                            request_body=normalized_body,
                            response_status=409,
                            response_body=response_body,
                        )
                    )

                    session.flush()

                    return JSONResponse(
                        status_code=409,
                        content=response_body,
                        media_type="application/problem+json",
                    )

                # 10. Move money

                source.balance_piastres -= amount

                destination.balance_piastres += amount

                # 11. Create transfer

                transfer = Transfer(
                    id="trf_" + uuid.uuid4().hex,
                    from_account_id=from_id,
                    to_account_id=to_id,
                    amount_piastres=amount,
                    memo=memo,
                    status="completed",
                )

                session.add(transfer)

                response_body = _transfer_json(transfer)

                # 12. Save idempotency record
                # Same transaction as money transfer

                session.add(
                    IdempotencyRecord(
                        customer_id=customer_id,
                        key=key,
                        request_body=normalized_body,
                        response_status=201,
                        response_body=response_body,
                    )
                )

                session.flush()

                return JSONResponse(
                    status_code=201,
                    content=response_body,
                )

    # ---------------------------------------
    # GET /v1/accounts/{account_id}
    # ---------------------------------------

    @app.get("/v1/accounts/{account_id}")
    def get_account(account_id: str, request: Request):

        customer_id = authenticate(request)

        if customer_id is None:

            return _problem(
                401,
                "Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

        with SessionLocal() as session:

            account = session.get(Account, account_id)

            if account is None or account.owner_id != customer_id:

                return _problem(
                    404,
                    "Not Found",
                    "Account not found.",
                )

            return {
                "id": account.id,
                "balance_piastres": account.balance_piastres,
                "currency": account.currency,
            }

    # ---------------------------------------
    # GET /v1/transfers/{transfer_id}
    # ---------------------------------------

    @app.get("/v1/transfers/{transfer_id}")
    def get_transfer(transfer_id: str, request: Request):

        customer_id = authenticate(request)

        if customer_id is None:

            return _problem(
                401,
                "Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

        with SessionLocal() as session:

            transfer = session.get(Transfer, transfer_id)

            if transfer is None:

                return _problem(
                    404,
                    "Not Found",
                    "Transfer not found.",
                )

            source = session.get(
                Account,
                transfer.from_account_id,
            )

            destination = session.get(
                Account,
                transfer.to_account_id,
            )

            # Only sender or receiver can see the transfer

            if not (
                (source is not None and source.owner_id == customer_id)
                or (
                    destination is not None
                    and destination.owner_id == customer_id
                )
            ):

                return _problem(
                    404,
                    "Not Found",
                    "Transfer not found.",
                )

            return _transfer_json(transfer)

    return app