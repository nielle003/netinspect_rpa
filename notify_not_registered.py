from datetime import datetime
import os
from dotenv import load_dotenv
load_dotenv()   
current_datetime = datetime.now()
DB_TABLE = os.getenv("DB_TABLE")

def send_notification(conn, part):
    """ THIS IS TO SEND THE NOTIFICATION TO THE CORRECT PEOPLE"""

    cursor = conn.cursor()

    notification_detail = f"Part number: {part.number}, Rev Id: {part.rev}, has not been registered in Net-Inspect."
    notification_key = f"{part.number}_{current_datetime.strftime('%Y-%m-%d_%H-%M-%S')}"
    notification_link = f"/V4/Others/Nielle/IT Kaizen/nielle_netinspectNotif.php?part_number={part.number}"
    notification_type = 67

    try:

        cursor.execute(
            """
            INSERT INTO system_notificationdetails
            (notificationDetail, notificationKey, notificationLink, notificationType)
            VALUES (%s, %s, %s, %s)
            """,
            (notification_detail, notification_key, notification_link, notification_type)
        )

        notification_detail_id = cursor.lastrowid

        #notify encoders
        cursor.execute(
            """
            INSERT INTO system_notification (notificationId, notificationTarget, notificationStatus, targetType)
            VALUES (%s, %s, %s, %s)
            """,
            (notification_detail_id, 34, 0, 1)
        )

       # notify sales
        cursor.execute(
            """
            INSERT INTO system_notification (notificationId, notificationTarget, notificationStatus, targetType)
            VALUES (%s, %s, %s, %s)
            """,
            (notification_detail_id, 54, 0, 1)
        )

        #set unregistered flag in the part table
        cursor.execute(
        
            f"UPDATE {DB_TABLE} SET isNotRegistered = 1 WHERE partNumber = %s AND revision = %s and isFinish = 0",
            (part.number, part.rev)    
        )

        conn.commit()
    except Exception as e:
        #print(f"Error committing notification details: {e}")
        conn.rollback()
        return False

    return True
