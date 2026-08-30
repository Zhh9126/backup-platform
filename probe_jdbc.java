import java.sql.Connection;
import java.sql.DatabaseMetaData;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;

public class JdbcTest {
    public static void main(String[] args) throws Exception {
        String url = args[0], user = args[1], pass = args[2], driver = args[3];
        DriverManager.setLoginTimeout(15);
        Class.forName(driver);
        long t0 = System.currentTimeMillis();
        Connection c = DriverManager.getConnection(url, user, pass);
        long ms = System.currentTimeMillis() - t0;
        DatabaseMetaData m = c.getMetaData();
        String probe;
        try (Statement s = c.createStatement()) {
            try (ResultSet r = s.executeQuery("SELECT 1 FROM dual")) {
                r.next(); probe = r.getString(1);
            } catch (Exception e) {
                try (ResultSet r = s.executeQuery("SELECT 1")) { r.next(); probe = r.getString(1); }
            }
        }
        System.out.println("JDBC_OK|" + m.getDatabaseProductName()
                + "|" + m.getDatabaseProductVersion().split("\n")[0]
                + "|probe=" + probe + "|connect_ms=" + ms);
        c.close();
    }
}
