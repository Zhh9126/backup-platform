# JDBC 驱动目录

存放各数据库 JDBC 驱动 jar，供平台 JDBC 连接方式使用。
该目录会随 PyInstaller 打包进 `dist/backup_platform(.exe)`，离线部署免下载。

## 驱动清单

| 数据库类型 | 驱动文件 | 驱动类 | 默认 URL 模板 |
|---|---|---|---|
| postgresql | postgresql-42.7.5.jar | org.postgresql.Driver | jdbc:postgresql://{host}:{port}/{db} |
| mysql | mysql-connector-j-8.4.0.jar | com.mysql.cj.jdbc.Driver | jdbc:mysql://{host}:{port}/{db} |
| mariadb | mariadb-java-client-3.4.1.jar | org.mariadb.jdbc.Driver | jdbc:mariadb://{host}:{port}/{db} |
| oracle | ojdbc11-23.4.0.24.05.jar | oracle.jdbc.OracleDriver | jdbc:oracle:thin:@//{host}:{port}/{db} |
| kingbase | kingbase8-8.6.0.jar（官方） | com.kingbase8.Driver | jdbc:kingbase8://{host}:{port}/{db} |
| dameng | DmJdbcDriver18-8.1.3.62.jar | dm.jdbc.driver.DmDriver | jdbc:dm://{host}:{port}/{db} |
| redis / mongodb | 无需 JDBC（python 原生驱动） | - | - |

> 金仓官方 kingbase8.jar 需从人大金仓官网获取，放入本目录后即自动启用；
> 缺失时平台自动降级用 PostgreSQL 驱动连接（金仓协议兼容）。

## 运行前提

JDBC 桥接需要本机 Java 运行时（JDK 8+，JRE 亦可）。探测顺序：

1. 冻结（打包）环境：可执行文件同目录下的 `jdk/`、`jre/`、`java/`、`runtime/`（离线部署推荐，
   将 JDK 目录整体改名放入即可，无需安装 Java）；
2. 系统环境变量 `JAVA_HOME` / `JDK_HOME`；
3. 常见安装路径（Windows: Program Files\Java、Eclipse Adoptium、~/.jdks；Linux: /usr/lib/jvm）。

Python 侧依赖 `jpype1`、`jaydebeapi`（已加入 requirements.txt）。

## 手动下载源（在线环境获取驱动后放入本目录）

- PostgreSQL: https://repo1.maven.org/maven2/org/postgresql/postgresql/
- MySQL: https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/
- MariaDB: https://repo1.maven.org/maven2/org/mariadb/jdbc/mariadb-java-client/
- Oracle: https://repo1.maven.org/maven2/com/oracle/database/jdbc/ojdbc11/
- 达梦: https://repo1.maven.org/maven2/com/dameng/DmJdbcDriver18/
- 金仓: 官网 https://www.kingbase.com.cn/ 下载 kingbase8 JDBC 驱动
