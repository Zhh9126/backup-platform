# -*- coding: utf-8 -*-
"""SQL Server 2005 兼容性回归。

现场根因（2026-11-21）：2005 实例逻辑备份报
「找不到存储过程 'sp_columns 100'」——微软 JDBC 驱动实现
DatabaseMetaData.getColumns() 时内部调用 sp_columns（2008 才提供）；
2008+ 实例正常。另一处：物理备份无条件带 COMPRESSION（2008 才支持）。

本测试保证：
  1) 元数据只走 INFORMATION_SCHEMA / sys.*，一旦回退 DatabaseMetaData 即失败
  2) 版本探测在 2005（无 ProductMajorVersion）下仍能解析出 major=9
  3) 物理备份按版本关闭 COMPRESSION
"""
import pytest
from core import sqlserver_jdbc


class _FakeCursor:
    def __init__(self,db): self.db=db; self._rows=[]
    def execute(self,sql,params=None):
        s=(sql or "").lower()
        p=list(params or [])
        if "productmajorversion" in s:
            if self.db.version.startswith("9."):
                raise Exception("Invalid property name 'ProductMajorVersion'")
            self._rows=[(int(self.db.version.split(".")[0]),)]
        elif "productversion" in s:
            self._rows=[(self.db.version,)]
        elif "information_schema.columns" in s:
            self._rows=[tuple(r) for r in self.db.columns.get((p[0],p[1]),[])]
        elif "sys.identity_columns" in s:
            self._rows=[(n,) for n in self.db.identities.get((p[0],p[1]),[])]
        elif "sys.key_constraints" in s:
            self._rows=[(n,) for n in self.db.pks.get((p[0],p[1]),[])]
        else:
            self._rows=[]
    def fetchone(self): return self._rows[0] if self._rows else None
    def fetchall(self): return list(self._rows)
    def close(self): pass


class _FakeConn:
    def __init__(self,version="9.00.5000.00",columns=None,identities=None,pks=None):
        self.version=version
        self.columns=columns or {}
        self.identities=identities or {}
        self.pks=pks or {}
    def cursor(self): return _FakeCursor(self)
    def getMetaData(self):
        raise AssertionError("不得使用 JDBC DatabaseMetaData：2005 上驱动会调用不存在的 sp_columns")
    def close(self): pass


def _conn_2005():
    return _FakeConn(
        version="9.00.5000.00",
        columns={("dbo","t1"):[
            ("id","int",None,10,0,"NO",""),
            ("name","nvarchar",50,None,None,"YES",None),
            ("amount","decimal",None,12,2,"YES",None),
            ("memo","varchar",-1,None,None,"YES",None),
            ("note","nvarchar",10,None,None,"YES","N'x'"),
        ]},
        identities={("dbo","t1"):["id"]},
        pks={("dbo","t1"):["id"]})


def test_jdbc_metadata_avoids_databasemetadata_on_2005():
    ddl,has_id=sqlserver_jdbc._table_ddl(_conn_2005(),"dbo","t1")
    assert has_id is True
    assert "[id] INT IDENTITY(1,1) NOT NULL" in ddl
    assert "[name] NVARCHAR(50)" in ddl
    assert "[amount] DECIMAL(12,2)" in ddl
    assert "[memo] VARCHAR(MAX)" in ddl
    assert "DEFAULT N'x'" in ddl
    assert "PRIMARY KEY ([id])" in ddl
    assert "CREATE TABLE [dbo].[t1]" in ddl


def test_jdbc_metadata_reports_missing_table_honestly():
    with pytest.raises(ValueError):
        sqlserver_jdbc._table_ddl(_conn_2005(),"dbo","no_such_table")


def test_server_version_parses_2005_and_modern():
    major,full=sqlserver_jdbc._server_version(_conn_2005())
    assert major==9 and full.startswith("9.00")
    c=_FakeConn(version="10.50.6000.34")
    major,_=sqlserver_jdbc._server_version(c)
    assert major==10


def test_compress_option_disabled_on_2005_only():
    from core.engines.sqlserver import SQLServerEngine
    eng=SQLServerEngine({"db_type":"sqlserver","host":"10.0.0.1","port":1433,
                         "username":"sa","password":"x","db_name":"demo"},
                        "/tmp/aidbm-mssql-test")
    assert eng._compress_option(9)==""
    assert eng._compress_option(10)=="COMPRESSION, "
    assert eng._compress_option(15)=="COMPRESSION, "
    assert eng._compress_option(0)=="COMPRESSION, "
