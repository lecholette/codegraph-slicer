import json

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main
from codegraph_slicer.slicer import slice_context


def test_jpa_entity_contracts_and_annotations_preserved_in_slice_xml(tmp_path, capsys):
    root = tmp_path / "repo"
    pkg = root / "src/main/java/com/acme/billing"
    pkg.mkdir(parents=True)
    extra_fields = "\n".join(
        f"    @Column(name = \"attr_{i}\")\n    private String attr{i};"
        for i in range(18)
    )
    (pkg / "InvoiceEntity.java").write_text(
        "package com.acme.billing;\n"
        "import jakarta.persistence.*;\n"
        "import java.util.List;\n"
        "import org.hibernate.annotations.SQLRestriction;\n"
        "@Entity(name = \"LogicalInvoice\")\n"
        "@Table(name = \"dw_invoices\", indexes = {@Index(name = \"idx_email\", columnList = \"tax_id\")})\n"
        "@SQLRestriction(\"status = 'ACTIVE' AND deleted_at IS NULL\")\n"
        "public class InvoiceEntity {\n"
        "    @Id\n"
        "    @Column(name = \"invoice_id\")\n"
        "    private Long invoiceId;\n"
        "    @Column(name = \"net_amount_usd_cents\", columnDefinition = \"NUMERIC(10,2\")\n"
        "    private Long netAmountCents;\n"
        "    @Column(name = \"inline_cents\") private Long inlineCents;\n"
        "    private String plainMemo;\n"
        "    @Column(\n"
        "        name = \"multiline_cents\",\n"
        "        nullable = false\n"
        "    )\n"
        "    private Long multilineCents;\n"
        "    @Column(name = \"tax_id\")\n"
        "    private String taxId;\n"
        "    @OneToMany(mappedBy = \"invoice\")\n"
        "    private List<PaymentAttemptEntity> paymentAttempts;\n"
        f"{extra_fields}\n"
        "    public Long getNetAmountCents() { return netAmountCents; }\n"
        "    public Long getInvoiceId() { return invoiceId; }\n"
        "    public String getTaxId() { return taxId; }\n"
        "}\n",
        encoding="utf-8",
    )
    (pkg / "PaymentAttemptEntity.java").write_text(
        "package com.acme.billing;\n"
        "import jakarta.persistence.*;\n"
        "@Entity\n"
        "@Table(name = \"dw_payment_attempts\")\n"
        "public class PaymentAttemptEntity {\n"
        "    @Id\n"
        "    @Column(name = \"attempt_id\")\n"
        "    private Long attemptId;\n"
        "    @ManyToOne\n"
        "    @JoinColumn(name = \"invoice_id\")\n"
        "    private InvoiceEntity invoice;\n"
        "}\n",
        encoding="utf-8",
    )
    (pkg / "InvoiceRepo.java").write_text(
        "package com.acme.billing;\n"
        "public class InvoiceRepo {\n"
        "    public Long compute(InvoiceEntity inv) {\n"
        "        return inv.getNetAmountCents();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
        res = slice_context(
            store,
            ["src/main/java/com/acme/billing/InvoiceRepo.java:InvoiceRepo.compute"],
            max_tokens=8000,
        )
        contracts = res.get("data_contracts")
        assert contracts
        inv_c = next(c for c in contracts if c["table"] == "dw_invoices")
        assert inv_c["filters"] == ["status = 'ACTIVE' AND deleted_at IS NULL"]
        assert inv_c["columns"]["netAmountCents"] == "net_amount_usd_cents"
        assert inv_c["columns"]["inlineCents"] == "inline_cents"
        assert "plainMemo" not in inv_c["columns"]
        assert inv_c["columns"]["multilineCents"] == "multiline_cents"
        assert "tax_id" in inv_c["pii_columns"]
        assert any("1:N" in rel and "paymentAttempts" in rel for rel in inv_c["relations"])

    assert main(
        [
            "slice",
            "--db",
            str(db),
            "src/main/java/com/acme/billing/InvoiceRepo.java:InvoiceRepo.compute",
            "--format",
            "xml",
        ]
    ) == 0
    xml_out = capsys.readouterr().out
    assert "<data_contracts>" in xml_out
    assert 'table="dw_invoices"' in xml_out
    assert "status = &#x27;ACTIVE&#x27; AND deleted_at IS NULL" in xml_out or "status = 'ACTIVE' AND deleted_at IS NULL" in xml_out
    assert "netAmountCents=net_amount_usd_cents" in xml_out
    assert "1:N paymentAttempts" in xml_out

    assert main(
        [
            "slice",
            "--db",
            str(db),
            "src/main/java/com/acme/billing/InvoiceRepo.java:InvoiceRepo.compute",
            "--format",
            "markdown",
        ]
    ) == 0
    md_out = capsys.readouterr().out
    assert "## Data Contracts" in md_out
    assert "table `dw_invoices`" in md_out
    assert "filters: `status = 'ACTIVE' AND deleted_at IS NULL`" in md_out
    assert "`netAmountCents=net_amount_usd_cents`" in md_out
    assert "`1:N paymentAttempts -> List<PaymentAttemptEntity>`" in md_out
    assert "pii: `tax_id`" in md_out


def test_python_sqlalchemy_model_emits_data_contract_and_plain_code_omits_it(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "models.py").write_text(
        "class OrderModel:\n"
        "    __tablename__: str = 'dw_orders'\n"
        "    __table_args__ = ({'schema': 'billing'},)\n"
        "    order_id = 1\n"
        "    billing_email = Column('email_address', str)\n"
        "    items = relationship('Item')\n"
        "    def helper(self) -> int:\n"
        "        return self.order_id\n\n"
        "def load_order(o: OrderModel) -> int:\n"
        "    return o.order_id\n\n"
        "def plain_math(x: int) -> int:\n"
        "    return x + 1\n",
        encoding="utf-8",
    )
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
        orm_res = slice_context(store, ["models:load_order"], max_tokens=2000)
        contract = orm_res["data_contracts"][0]
        assert contract["table"] == "dw_orders"
        assert contract["columns"]["order_id"] == "order_id"
        assert contract["columns"]["billing_email"] == "email_address"
        assert "__table_args__" not in contract["columns"]
        assert "items" not in contract["columns"]
        assert "email_address" in contract["pii_columns"]

        plain_res = slice_context(store, ["models:plain_math"], max_tokens=2000)
        assert "data_contracts" not in plain_res
