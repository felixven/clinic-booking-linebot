# test_run_reminder.py
from app import app
from flows_reminders import run_reminder_check

if __name__ == "__main__":
    with app.app_context():
        # 假設你要測「兩天前提醒」，也就是 Appointment Date = today + 2
        processed_groups = run_reminder_check(days_before=3)
        print(f"run_reminder_check 完成，enqueue 的 group job 數量 = {processed_groups}")
