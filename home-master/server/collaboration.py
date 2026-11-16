"""Совместные исполнители, персональные отчёты и ручные резервы склада."""
import datetime as dt, json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, select, func
from sqlalchemy.orm import Mapped, Session, mapped_column
import server as core
from server import ADMINS, EXECUTOR, OWNER, STAFF_ROLES, Base, Order, User, current_user, get_db, get_order, log, need, now

router=APIRouter()
CAPS={"order_assign","warehouse_reservation_view","reservation_payment_confirm","reservation_period_manage"}

class OrderParticipant(Base):
 __tablename__="order_participants"
 id:Mapped[int]=mapped_column(primary_key=True)
 order_id:Mapped[int]=mapped_column(ForeignKey("orders.id"),index=True)
 user_id:Mapped[int]=mapped_column(ForeignKey("users.id"),index=True)
 assigned_by:Mapped[int]=mapped_column(ForeignKey("users.id"))
 method:Mapped[str]=mapped_column(String(16),default="assigned")
 assigned_at:Mapped[dt.datetime]=mapped_column(DateTime,default=now)
 ended_at:Mapped[Optional[dt.datetime]]=mapped_column(DateTime,nullable=True)
 active:Mapped[bool]=mapped_column(Boolean,default=True)

class ExecutorReport(Base):
 __tablename__="executor_reports"
 id:Mapped[int]=mapped_column(primary_key=True)
 order_id:Mapped[int]=mapped_column(ForeignKey("orders.id"),index=True)
 executor_id:Mapped[int]=mapped_column(ForeignKey("users.id"),index=True)
 work_done:Mapped[str]=mapped_column(Text,default="")
 comments:Mapped[str]=mapped_column(Text,default="")
 materials_json:Mapped[str]=mapped_column(Text,default="[]")
 labor_hours:Mapped[float]=mapped_column(Float,default=0)
 other_cost:Mapped[float]=mapped_column(Float,default=0)
 status:Mapped[str]=mapped_column(String(12),default="draft")
 created_at:Mapped[dt.datetime]=mapped_column(DateTime,default=now)
 updated_at:Mapped[dt.datetime]=mapped_column(DateTime,default=now)

class TrustedCapability(Base):
 __tablename__="trusted_capabilities"
 id:Mapped[int]=mapped_column(primary_key=True)
 user_id:Mapped[int]=mapped_column(ForeignKey("users.id"),unique=True,index=True)
 capabilities_json:Mapped[str]=mapped_column(Text,default="[]")
 updated_by:Mapped[int]=mapped_column(ForeignKey("users.id"))
 updated_at:Mapped[dt.datetime]=mapped_column(DateTime,default=now)

class Reservation(Base):
 __tablename__="wh_reservations"
 id:Mapped[int]=mapped_column(primary_key=True)
 item_id:Mapped[int]=mapped_column(ForeignKey("wh_items.id"),index=True)
 order_id:Mapped[int]=mapped_column(ForeignKey("orders.id"),index=True)
 user_id:Mapped[int]=mapped_column(ForeignKey("users.id"),index=True)
 qty:Mapped[float]=mapped_column(Float)
 status:Mapped[str]=mapped_column(String(12),default="active")
 paid_at:Mapped[Optional[dt.datetime]]=mapped_column(DateTime,nullable=True)
 confirmed_by:Mapped[Optional[int]]=mapped_column(ForeignKey("users.id"),nullable=True)
 expires_at:Mapped[Optional[dt.datetime]]=mapped_column(DateTime,nullable=True)
 created_at:Mapped[dt.datetime]=mapped_column(DateTime,default=now)

def capability(db,user,key):
 if user.role in ADMINS:return True
 row=db.scalar(select(TrustedCapability).where(TrustedCapability.user_id==user.id))
 try:return bool(row and key in json.loads(row.capabilities_json or "[]"))
 except:return False

def require_cap(db,user,key):
 if not capability(db,user,key):raise HTTPException(403,"Недостаточно прав")

# Исполнители могут просматривать все заказы; выполнять действия — только будучи участником.
_original_visible=core.visible_orders
def _visible(user):
 return select(Order) if user.role==EXECUTOR else _original_visible(user)
core.visible_orders=_visible

def participant(db,oid,uid):
 return db.scalar(select(OrderParticipant).where(OrderParticipant.order_id==oid,OrderParticipant.user_id==uid,OrderParticipant.active.is_(True)))

def p_out(p,db):
 u=db.get(User,p.user_id); a=db.get(User,p.assigned_by)
 return {"id":p.id,"user_id":p.user_id,"name":(u.full_name or u.username) if u else "?","assigned_by":(a.full_name or a.username) if a else "?","method":p.method,"assigned_at":p.assigned_at.isoformat(),"ended_at":p.ended_at.isoformat() if p.ended_at else None,"active":p.active}

class CapabilityIn(BaseModel): capabilities:list[str]=Field(default_factory=list)
@router.put("/trusted-users/{uid}/capabilities")
def set_caps(uid:int,b:CapabilityIn,user:User=Depends(current_user),db:Session=Depends(get_db)):
 need(user,OWNER); target=db.get(User,uid)
 if not target or target.role in ADMINS or target.role not in STAFF_ROLES:raise HTTPException(404,"Сотрудник не найден")
 if set(b.capabilities)-CAPS:raise HTTPException(400,"Неизвестное право")
 row=db.scalar(select(TrustedCapability).where(TrustedCapability.user_id==uid))
 if row is None:row=TrustedCapability(user_id=uid,updated_by=user.id);db.add(row)
 row.capabilities_json=json.dumps(sorted(set(b.capabilities)));row.updated_by=user.id;row.updated_at=now()
 log(db,user,"trusted_capabilities","user",uid,row.capabilities_json);db.commit()
 return {"user_id":uid,"capabilities":json.loads(row.capabilities_json)}

@router.get("/trusted-users")
def get_caps(user:User=Depends(current_user),db:Session=Depends(get_db)):
 need(user,OWNER)
 return [{"user_id":r.user_id,"capabilities":json.loads(r.capabilities_json or "[]"),"updated_at":r.updated_at.isoformat()} for r in db.scalars(select(TrustedCapability))]

class ParticipantIn(BaseModel): executor_id:int
@router.get("/orders/{oid}/participants")
def get_participants(oid:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
 get_order(db,user,oid)
 return [p_out(p,db) for p in db.scalars(select(OrderParticipant).where(OrderParticipant.order_id==oid).order_by(OrderParticipant.id))]

@router.post("/orders/{oid}/participants")
def add_participant(oid:int,b:ParticipantIn,user:User=Depends(current_user),db:Session=Depends(get_db)):
 o=get_order(db,user,oid)
 if user.role not in ADMINS and not capability(db,user,"order_assign"):raise HTTPException(403,"Недостаточно прав")
 ex=db.get(User,b.executor_id)
 if not ex or ex.role!=EXECUTOR or not ex.is_active or (ex.verification or "verified")!="verified":raise HTTPException(400,"Исполнитель не найден или не проверен")
 if participant(db,oid,ex.id):return p_out(participant(db,oid,ex.id),db)
 p=OrderParticipant(order_id=oid,user_id=ex.id,assigned_by=user.id,method="assigned");db.add(p)
 if o.executor_id is None:o.executor_id=ex.id
 if o.status=="new":o.status="assigned"
 db.flush();log(db,user,"assign_executor","order",oid,f"executor={ex.id}; participant={p.id}");db.commit()
 return p_out(p,db)

@router.delete("/orders/{oid}/participants/{uid}")
def remove_participant(oid:int,uid:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
 o=get_order(db,user,oid)
 if user.role not in ADMINS and not capability(db,user,"order_assign"):raise HTTPException(403,"Недостаточно прав")
 p=participant(db,oid,uid)
 if not p:raise HTTPException(404,"Активное назначение не найдено")
 p.active=False;p.ended_at=now()
 if o.executor_id==uid:
  nxt=db.scalar(select(OrderParticipant).where(OrderParticipant.order_id==oid,OrderParticipant.active.is_(True),OrderParticipant.user_id!=uid).order_by(OrderParticipant.id))
  o.executor_id=nxt.user_id if nxt else None
 log(db,user,"unassign_executor","order",oid,f"executor={uid}");db.commit()
 return {"ok":True}

@router.post("/orders/{oid}/self-claim")
def self_claim(oid:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
 need(user,EXECUTOR)
 if (user.verification or "verified")!="verified":raise HTTPException(403,"Учётная запись не проверена")
 o=get_order(db,user,oid)
 if o.status in ("done","cancelled"):raise HTTPException(409,"Заказ закрыт")
 if participant(db,oid,user.id):return {"ok":True,"already_participant":True}
 p=OrderParticipant(order_id=oid,user_id=user.id,assigned_by=user.id,method="self_claim");db.add(p)
 if o.executor_id is None:o.executor_id=user.id
 if o.status=="new":o.status="accepted"
 log(db,user,"self_claim_order","order",oid,f"executor={user.id}");db.commit()
 return {"ok":True,"participant":p_out(p,db)}

class ReportIn(BaseModel):
 work_done:str=Field(default="",max_length=20000)
 comments:str=Field(default="",max_length=20000)
 materials:list[dict]=Field(default_factory=list)
 labor_hours:float=Field(default=0,ge=0,le=10000)
 other_cost:float=Field(default=0,ge=0,le=100000000)
 submit:bool=False

def report_out(r,db):
 u=db.get(User,r.executor_id)
 return {"id":r.id,"executor_id":r.executor_id,"executor_name":(u.full_name or u.username) if u else "?","work_done":r.work_done,"comments":r.comments,"materials":json.loads(r.materials_json or "[]"),"labor_hours":r.labor_hours,"other_cost":r.other_cost,"status":r.status,"updated_at":r.updated_at.isoformat()}

@router.get("/orders/{oid}/reports")
def get_reports(oid:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
 get_order(db,user,oid)
 rows=db.scalars(select(ExecutorReport).where(ExecutorReport.order_id==oid).order_by(ExecutorReport.id)).all()
 if user.role==EXECUTOR:
  if not participant(db,oid,user.id):raise HTTPException(403,"Вы не участник заказа")
  rows=[r for r in rows if r.executor_id==user.id]
 return [report_out(r,db) for r in rows]

@router.put("/orders/{oid}/reports/me")
def put_report(oid:int,b:ReportIn,user:User=Depends(current_user),db:Session=Depends(get_db)):
 need(user,EXECUTOR);o=get_order(db,user,oid)
 if not participant(db,oid,user.id):raise HTTPException(403,"Вы не назначены на заказ")
 if o.settled_at:raise HTTPException(409,"Заказ рассчитан")
 for m in b.materials:
  if not isinstance(m,dict) or not m.get("name") or float(m.get("qty",0))<=0:raise HTTPException(400,"Некорректный материал")
 r=db.scalar(select(ExecutorReport).where(ExecutorReport.order_id==oid,ExecutorReport.executor_id==user.id))
 if r is None:r=ExecutorReport(order_id=oid,executor_id=user.id);db.add(r)
 if r.status=="submitted":raise HTTPException(409,"Отправленный отчёт нельзя изменить")
 r.work_done=b.work_done;r.comments=b.comments;r.materials_json=json.dumps(b.materials,ensure_ascii=False)
 r.labor_hours=b.labor_hours;r.other_cost=b.other_cost;r.status="submitted" if b.submit else "draft";r.updated_at=now()
 log(db,user,"executor_report_"+r.status,"order",oid);db.commit()
 return report_out(r,db)

class ReserveIn(BaseModel):item_id:int;order_id:int;qty:float=Field(gt=0,le=100000000)
def expire(db,item_id=None):
 q=select(Reservation).where(Reservation.status=="active",Reservation.paid_at.is_(None),Reservation.expires_at.is_not(None),Reservation.expires_at<=now())
 if item_id:q=q.where(Reservation.item_id==item_id)
 for r in db.scalars(q):r.status="expired";log(db,None,"reservation_expired","reservation",r.id)
def r_out(r,db,priv):
 from warehouse import Item
 it=db.get(Item,r.item_id)
 d={"id":r.id,"item_id":r.item_id,"item_name":it.name if it else "?","order_id":r.order_id,"qty":r.qty,"status":r.status,"paid":bool(r.paid_at),"expires_at":r.expires_at.isoformat() if r.expires_at else None}
 if priv:
  u=db.get(User,r.user_id);c=db.get(User,r.confirmed_by) if r.confirmed_by else None
  d.update(user_id=r.user_id,reserved_by=(u.full_name or u.username) if u else "?",confirmed_by=(c.full_name or c.username) if c else None,paid_at=r.paid_at.isoformat() if r.paid_at else None)
 return d

@router.post("/warehouse/reservations")
def reserve(b:ReserveIn,user:User=Depends(current_user),db:Session=Depends(get_db)):
 need(user,*STAFF_ROLES);o=get_order(db,user,b.order_id)
 if user.role==EXECUTOR and not participant(db,o.id,user.id):raise HTTPException(403,"Только участник заказа может резервировать материалы")
 from warehouse import Item,Stock
 it=db.get(Item,b.item_id)
 if not it or not it.active:raise HTTPException(404,"Позиция не найдена")
 expire(db,it.id)
 stock=float(db.scalar(select(func.coalesce(func.sum(Stock.qty),0.0)).where(Stock.item_id==it.id,Stock.holder_user_id.is_(None))) or 0)
 held=sum(float(x.qty) for x in db.scalars(select(Reservation).where(Reservation.item_id==it.id,Reservation.status=="active")))
 avail=max(0,stock-held)
 if avail+1e-9<b.qty:raise HTTPException(409,f"Доступно только {avail:g} {it.unit}")
 hours=24;setting=db.get(core.AppSetting,"reservation_hours")
 if setting and setting.value:
  try:hours=max(1,min(720,int(json.loads(setting.value))))
  except:pass
 r=Reservation(item_id=it.id,order_id=o.id,user_id=user.id,qty=b.qty,expires_at=now()+dt.timedelta(hours=hours));db.add(r);db.flush()
 log(db,user,"warehouse_reserve","reservation",r.id,f"item={it.id}; qty={b.qty}; order={o.id}; expires={r.expires_at.isoformat()}")
 db.commit();return r_out(r,db,True)

@router.get("/warehouse/reservations")
def reservations(order_id:Optional[int]=None,user:User=Depends(current_user),db:Session=Depends(get_db)):
 need(user,*STAFF_ROLES);expire(db)
 priv=capability(db,user,"warehouse_reservation_view")
 q=select(Reservation).where(Reservation.status=="active")
 if order_id:q=q.where(Reservation.order_id==order_id)
 if user.role==EXECUTOR and not priv:q=q.where(Reservation.user_id==user.id)
 rows=db.scalars(q.order_by(Reservation.id.desc()).limit(1000)).all();db.commit()
 return [r_out(r,db,priv) for r in rows]

@router.post("/warehouse/reservations/{rid}/confirm-payment")
def confirm_payment(rid:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
 if not capability(db,user,"reservation_payment_confirm"):raise HTTPException(403,"Недостаточно прав")
 expire(db);r=db.get(Reservation,rid)
 if not r or r.status!="active":raise HTTPException(404,"Активный резерв не найден")
 if not r.paid_at:r.paid_at=now();r.confirmed_by=user.id;r.expires_at=None;log(db,user,"reservation_payment_confirmed","reservation",r.id,f"order={r.order_id}; item={r.item_id}; user={r.user_id}; qty={r.qty}")
 db.commit();return r_out(r,db,True)

@router.delete("/warehouse/reservations/{rid}")
def cancel_reservation(rid:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
 r=db.get(Reservation,rid)
 if not r or r.status!="active":raise HTTPException(404,"Резерв не найден")
 if r.user_id!=user.id and not capability(db,user,"reservation_payment_confirm"):raise HTTPException(403,"Недостаточно прав")
 r.status="cancelled";log(db,user,"reservation_cancelled","reservation",r.id);db.commit();return {"ok":True}

class PeriodIn(BaseModel):hours:int=Field(ge=1,le=720)
@router.put("/warehouse/reservations/settings/period")
def period(b:PeriodIn,user:User=Depends(current_user),db:Session=Depends(get_db)):
 need(user,OWNER);row=db.get(core.AppSetting,"reservation_hours")
 if row is None:row=core.AppSetting(key="reservation_hours",value="24");db.add(row)
 row.value=json.dumps(b.hours);log(db,user,"reservation_period_changed","settings",None,f"hours={b.hours}");db.commit();return {"hours":b.hours}

@router.get("/warehouse/availability/{item_id}")
def availability(item_id:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
 need(user,*STAFF_ROLES)
 from warehouse import Item,Stock
 it=db.get(Item,item_id)
 if not it:raise HTTPException(404)
 expire(db,item_id);stock=float(db.scalar(select(func.coalesce(func.sum(Stock.qty),0.0)).where(Stock.item_id==item_id,Stock.holder_user_id.is_(None))) or 0)
 held=sum(float(x.qty) for x in db.scalars(select(Reservation).where(Reservation.item_id==item_id,Reservation.status=="active")))
 db.commit();return {"item_id":item_id,"total":stock,"reserved":held,"available":max(0,stock-held),"unit":it.unit}
