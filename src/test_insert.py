import os
import psycopg2
from dotenv import load_dotenv, find_dotenv

# Force load the .env file
load_dotenv(find_dotenv())

def test_single_insert():
    try:
        # Open a connection to your database
        conn = psycopg2.connect(
            host="localhost",
            port=5432,
            database="SAL-db",
            user="postgres",
            password=os.getenv("POSTGRES_PASSWORD")
        )
        cursor = conn.cursor()

        # Create a test table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_table (
                id SERIAL PRIMARY KEY,
                title VARCHAR(255) NOT NULL
            );
        """)
        conn.commit()  # Commit table creation

        # Insert a test row into the test table
        cursor.execute("""
            INSERT INTO test_table (title) 
            VALUES (%s) 
            RETURNING id;
        """, ("Test Title",))
        new_id = cursor.fetchone()[0]
        conn.commit()  # Commit the insert
        print("Inserted row with id:", new_id)

        # Query the inserted row to verify
        cursor.execute("SELECT id, title FROM test_table WHERE id = %s;", (new_id,))
        row = cursor.fetchone()
        print("Retrieved row:", row)

    except Exception as e:
        print("An error occurred:", e)
    finally:
        # Always close cursor and connection
        if cursor:
            cursor.close()
        if conn:
            conn.close()

if __name__ == "__main__":
    test_single_insert()
