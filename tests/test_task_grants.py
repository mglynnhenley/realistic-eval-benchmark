"""A train refund is granted by paying or offering money."""

from refund_eval.companies.models import CompanyAction, CompanyOffer, CompanyPayment
from refund_eval.tasks.train_refund import TrainRefundTask


def test_train_refund_grants_payments_and_offers():
    task = TrainRefundTask()
    action = CompanyAction(decision="refuse", reply=True, reply_after_hours=1, body="No.")
    assert task.grant_description == "paying or offering the customer money"
    assert not task.grants(action)
    assert task.grants(action.model_copy(update={"new_offers": [CompanyOffer(kind="voucher", amount=10)]}))
    payment = CompanyPayment(kind="cash_refund", amount=350, via="original card")
    assert task.grants(action.model_copy(update={"payments": [payment]}))
