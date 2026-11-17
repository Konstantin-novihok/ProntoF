from pathlib import Path
import hashlib

from fastapi.testclient import TestClient
import server


def test_order_form_reference_ids_and_complete_file_upload():
    # DATA_DIR and ORDER_DATA_DIR are temporary CI paths, never a deployed database.
    with TestClient(server.app) as client:
        with server.SessionLocal() as db:
            owner = db.scalar(server.select(server.User).where(server.User.role == server.OWNER))
            assert owner is not None, "test startup must create the isolated owner"
            customer = server.User(
                username="ci_order_customer",
                password_hash=server.hash_password("ci-customer-password"),
                role=server.CLIENT,
                full_name="CI Customer",
                company_name="CI Test Company",
            )
            department = server.Department(name="CI Test Direction")
            db.add_all([customer, department])
            db.commit()
            customer_id, department_id, owner_id = customer.id, department.id, owner.id
            token = server.make_token(owner)

        headers = {"Authorization": "Bearer " + token}
        departments = client.get("/departments", headers=headers)
        customers = client.get("/users?role=4", headers=headers)
        assert departments.status_code == 200, departments.text
        assert customers.status_code == 200, customers.text
        assert any(x["id"] == department_id for x in departments.json())
        assert any(x["id"] == customer_id for x in customers.json())

        created = client.post("/orders", headers=headers, json={
            "title": "CI order reference IDs",
            "description": "isolated test",
            "address": "CI only",
            "service_type": "Diagnostics",
            "department_id": department_id,
            "client_id": customer_id,
        })
        assert created.status_code == 200, created.text
        order_id = created.json()["id"]
        with server.SessionLocal() as db:
            order = db.get(server.Order, order_id)
            assert order is not None
            assert order.created_by == owner_id
            assert order.department_id == department_id
            assert order.client_id == customer_id

        # Upload a complete binary attachment in one multipart request and verify
        # the exact bytes on disk and the media row in the isolated test database.
        payload = (b"PRONTOF-CI-VIDEO\x00" + bytes(range(256))) * 2048
        upload_key = "ci-complete-video-upload"
        upload_headers = {
            **headers,
            "X-Client-Upload-ID": upload_key,
            "X-Content-Size": str(len(payload)),
            "X-Content-SHA256": hashlib.sha256(payload).hexdigest(),
        }
        uploaded = client.post(
            f"/orders/{order_id}/media",
            headers=upload_headers,
            files={"file": ("ci-original.mp4", payload, "video/mp4")},
        )
        assert uploaded.status_code == 200, uploaded.text
        media_id = uploaded.json()["id"]
        with server.SessionLocal() as db:
            media = db.get(server.Media, media_id)
            assert media is not None and media.order_id == order_id
            stored_path = Path(server.media_path(media))
            assert stored_path.is_file()
            assert stored_path.stat().st_size == len(payload)
            assert stored_path.read_bytes() == payload
        assert uploaded.json()["size"] == len(payload)
        print("Order reference IDs and complete single-request video attachment passed on isolated SQLite.")
