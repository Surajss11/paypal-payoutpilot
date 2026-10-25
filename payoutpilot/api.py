from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent
from . import llm as llm_mod
from . import paypal as paypal_mod
from . import pipeline
from .config import load_env_files
from .models import Campaign, Submission
from .store import CampaignState, Store

WEB = Path(__file__).resolve().parent.parent / "web"


class Approval(BaseModel):
    confirm_total: Decimal
    approved_by: str = "dashboard user"


class PayoutRequest(BaseModel):
    text: str
    title: str = ""


def create_app(store: Store | None = None, llm=None, paypal=None) -> FastAPI:
    app = FastAPI(title="PayoutPilot", version="0.2.0")
    app.state.store = store or Store()
    app.state.llm = llm
    app.state.paypal = paypal

    def get_llm():
        if app.state.llm is None:
            app.state.llm = llm_mod.client_from_env()
        return app.state.llm

    def get_paypal():
        if app.state.paypal is None:
            app.state.paypal = paypal_mod.client_from_env()
        return app.state.paypal

    def load(cid: str) -> CampaignState:
        try:
            return app.state.store.get(cid)
        except KeyError:
            raise HTTPException(404, f"campaign {cid} not found") from None

    def view(st: CampaignState) -> dict:
        d = st.model_dump(mode="json")
        d["excluded"] = sorted(pipeline.excluded(st))
        d["plan_total"] = str(st.plan.total) if st.plan else None
        return d

    @app.get("/api/health")
    def health():
        try:
            pp = get_paypal().mode
        except RuntimeError as e:
            pp = f"not configured ({e})"
        return {"ok": True, "llm": getattr(get_llm(), "model", "?"), "paypal": pp}

    @app.get("/api/campaigns")
    def list_campaigns():
        return app.state.store.list()

    @app.post("/api/campaigns")
    def create_campaign(c: Campaign):
        st = CampaignState(campaign=c)
        app.state.store.put(st)
        return view(st)

    @app.get("/api/campaigns/{cid}")
    def get_campaign(cid: str):
        return view(load(cid))

    @app.post("/api/campaigns/{cid}/submissions")
    def add_submissions(cid: str, subs: list[Submission]):
        st = load(cid)
        if st.payout_batch_id:
            raise HTTPException(400, "already paid; start a new run for new submissions")
        st.submissions = subs
        pipeline.run_checks(st)
        st.scores, st.plan = [], None
        app.state.store.put(st)
        return view(st)

    @app.post("/api/campaigns/{cid}/submissions.csv")
    async def upload_csv(cid: str, file: UploadFile = File(...)):
        return add_submissions(cid, pipeline.parse_csv((await file.read()).decode("utf-8-sig")))

    @app.post("/api/campaigns/{cid}/judge")
    def judge(cid: str, review: bool = True):
        def go(st):
            pipeline.run_checks(st)
            pipeline.run_judge(st, get_llm(), review=review)
            pipeline.make_plan(st)
        return act(cid, go)

    @app.post("/api/campaigns/{cid}/exclude")
    def exclude(cid: str, ids: list[str] = Body(...)):
        def go(st):
            if st.payout_batch_id:
                raise ValueError("already paid; exclusions can no longer change the plan")
            st.manual_excluded = sorted(set(ids))
            pipeline.make_plan(st)
        return act(cid, go)

    @app.post("/api/campaigns/{cid}/approve")
    def approve(cid: str, a: Approval):
        st = load(cid)
        try:
            pipeline.approve_and_pay(st, get_paypal(), a.confirm_total, a.approved_by[:80])
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except paypal_mod.PayPalError as e:
            raise HTTPException(502, str(e)) from None
        app.state.store.put(st)
        return view(st)

    def act(cid: str, fn):
        st = load(cid)
        try:
            fn(st)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except paypal_mod.PayPalError as e:
            raise HTTPException(502, str(e)) from None
        app.state.store.put(st)
        return view(st)

    @app.post("/api/campaigns/{cid}/refresh")
    def refresh(cid: str, wait: bool = False):
        return act(cid, lambda st: pipeline.refresh_status(st, get_paypal(), wait=wait, timeout=45))

    @app.post("/api/campaigns/{cid}/retry")
    def retry(cid: str):
        return act(cid, lambda st: pipeline.retry_failed(st, get_paypal()))

    @app.post("/api/campaigns/{cid}/cancel-unclaimed")
    def cancel_unclaimed(cid: str):
        def go(st):
            pipeline.cancel_unclaimed(st, get_paypal())
            pipeline.refresh_status(st, get_paypal())
        return act(cid, go)

    @app.post("/api/campaigns/{cid}/funding-order")
    def funding_order(cid: str):
        return act(cid, lambda st: pipeline.create_funding_order(st, get_paypal()))

    @app.post("/api/campaigns/{cid}/funding-order/refresh")
    def funding_order_refresh(cid: str, capture: bool = False):
        return act(cid, lambda st: pipeline.refresh_funding_order(st, get_paypal(), capture=capture))

    @app.post("/api/requests")
    def new_request(req: PayoutRequest):
        if not req.text.strip():
            raise HTTPException(400, "empty request")
        states = []
        for cid in app.state.store.list():
            try:
                states.append(app.state.store.get(cid))
            except Exception:
                continue
        st = agent.plan_request(req.text, get_llm(), pipeline.payee_directory(states), req.title)
        app.state.store.put(st)
        return view(st)

    if WEB.exists():
        app.mount("/static", StaticFiles(directory=WEB), name="static")

        @app.get("/")
        def index():
            return FileResponse(WEB / "index.html")

    return app


load_env_files()
app = create_app()
