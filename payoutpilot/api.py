from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import llm as llm_mod
from . import paypal as paypal_mod
from . import pipeline
from .models import Campaign, Submission
from .store import CampaignState, Store

WEB = Path(__file__).resolve().parent.parent / "web"


class Approval(BaseModel):
    confirm_total: Decimal


def create_app(store: Store | None = None, llm=None, paypal=None) -> FastAPI:
    app = FastAPI(title="PayoutPilot", version="0.1.0")
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
        return {"ok": True, "llm": getattr(get_llm(), "model", "?"),
                "paypal": "mock" if isinstance(get_paypal(), paypal_mod.MockPayPal) else get_paypal().base_url}

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
        st = load(cid)
        pipeline.run_checks(st)
        pipeline.run_judge(st, get_llm(), review=review)
        pipeline.make_plan(st)
        app.state.store.put(st)
        return view(st)

    @app.post("/api/campaigns/{cid}/exclude")
    def exclude(cid: str, ids: list[str] = Body(...)):
        st = load(cid)
        st.manual_excluded = sorted(set(ids))
        pipeline.make_plan(st)
        app.state.store.put(st)
        return view(st)

    @app.post("/api/campaigns/{cid}/approve")
    def approve(cid: str, a: Approval):
        st = load(cid)
        try:
            pipeline.approve_and_pay(st, get_paypal(), a.confirm_total)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except paypal_mod.PayPalError as e:
            raise HTTPException(502, str(e)) from None
        app.state.store.put(st)
        return view(st)

    @app.post("/api/campaigns/{cid}/refresh")
    def refresh(cid: str):
        st = load(cid)
        try:
            pipeline.refresh_status(st, get_paypal())
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        app.state.store.put(st)
        return view(st)

    if WEB.exists():
        app.mount("/static", StaticFiles(directory=WEB), name="static")

        @app.get("/")
        def index():
            return FileResponse(WEB / "index.html")

    return app


app = create_app()
