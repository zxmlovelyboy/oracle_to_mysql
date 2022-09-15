# -*- coding: utf-8 -*-
import argparse
import datetime
import decimal
import logging
import multiprocessing
import os
import platform
import re
import sys
import textwrap
import time
import traceback
import configDB
import cx_Oracle
import pymysql
from db_info import get_info, run_info, tbl_columns, cte_tab, cte_idx, fk, cte_trg, cte_comt, c_vw, func_proc, cp_vw

"""
Oracle database migration to MySQL
py37
V1.9.15.2 2022-09-15
精简代码，添加部分注释
"""
version = '1.9.15.2'
release_date = ' 2022-09-15'


parser = argparse.ArgumentParser(prog='oracle_mig_mysql',
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description=textwrap.dedent('''\

EXAMPLE:
    EG(1):RUN MIGRATION FETCH AND INSERT 10000 ROWS DATA INTO TABLE:\n ./oracle_to_mysql -b 10000\n
    EG(2):RUN IMPORT CUSTOM TABLE MIGRATION TO MySQL INCLUDE METADATA AND DATA ROWS:\n ./oracle_to_mysql -c true\n
    '''))
parser.add_argument('--batch_size', '-b', help='FETCH AND INSERT ROW SIZE,DEFAULT 10000', type=int)
parser.add_argument('--custom_table', '-c', help='MIG CUSTOM TABLES INTO MySQL,DEFAULT FALSE', action='store_true',
                    default='false')  # 默认是全表迁移
parser.add_argument('--data_only', '-d', help='MIG ONLY DATA ROW DO NOT CREATE TABLE', action='store_true',
                    default='false')
parser.add_argument('--metadata_only', '-m', help='MIG ONLY METADATA', action='store_true', default='false')
parser.add_argument('--parallel_degree', '-p', help='parallel degree default 2', type=int)
parser.add_argument('--split_page', '-s', help='split page default 10000', type=int)
parser.add_argument('--quite_mode', '-q', help='quite mode mig', action='store_true',
                    default='false')
parser.add_argument('-v', '--version', action='version', version=version + release_date, help='Display version')
args, unparsed = parser.parse_known_args()  # 只解析正确的参数列表，无效参数会被忽略且不报错，args是解析正确参数，unparsed是不被解析的错误参数，win多进程需要此写法
# args = parser.parse_args()

#  分页的记录数，默认每次分页的记录为10000条,即每次获取的行数
if args.split_page:
    split_page_size = args.split_page
else:
    split_page_size = 10000

# -c命令与-d命令不能同时使用的判断
if str(args.custom_table).upper() == 'TRUE' and str(args.data_only).upper() == 'TRUE':
    print('ERROR: -c AND -d OPTION CAN NOT BE USED TOGETHER!\nEXIT')
    sys.exit(0)

# -c命令与-m命令不能同时使用的判断
if str(args.custom_table).upper() == 'TRUE' and str(args.metadata_only).upper() == 'TRUE':
    print('ERROR: -c AND -m OPTION CAN NOT BE USED TOGETHER!\nEXIT')
    sys.exit(0)

# -m命令与-d命令不能同时使用的判断
if str(args.data_only).upper() == 'TRUE' and str(args.metadata_only).upper() == 'TRUE':
    print('ERROR: -d AND -m OPTION CAN NOT BE USED TOGETHER!\nEXIT')
    sys.exit(0)

# 判断命令行参数-b是否指定,即每次插入的行数
if args.batch_size:
    row_batch_size = args.batch_size
else:
    row_batch_size = 10000

# 记录执行日志
class Logger(object):
    def __init__(self, filename='default.log', add_flag=True, stream=sys.stdout):
        self.terminal = stream
        self.filename = filename
        self.add_flag = add_flag

    def write(self, message):
        if self.add_flag:
            with open(self.filename, 'a+') as log:
                self.terminal.write(message)
                log.write(message)
        else:
            with open(self.filename, 'w') as log:
                self.terminal.write(message)
                log.write(message)

    def flush(self):
        pass


# clob、blob、nclob要在读取源表前加载outputtypehandler属性,即将Oracle大字段转为string类型
# 处理Oracle的number类型浮点数据与Python decimal类型的转换
# Python遇到超过3位小数的浮点类型，小数部分只能保留3位，其余会被截断，会造成数据不准确，需要用此handler做转换，可指定数据库连接或者游标对象
def dataconvert(cursor, name, defaultType, size, precision, scale):
    if defaultType == cx_Oracle.DB_TYPE_CLOB:
        return cursor.var(cx_Oracle.DB_TYPE_LONG, arraysize=cursor.arraysize)
    if defaultType == cx_Oracle.DB_TYPE_BLOB:
        return cursor.var(cx_Oracle.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)
    if defaultType == cx_Oracle.DB_TYPE_NCLOB:
        return cursor.var(cx_Oracle.DB_TYPE_LONG, arraysize=cursor.arraysize)
    if defaultType == cx_Oracle.DB_TYPE_NUMBER:  # NumberToDecimal
        return cursor.var(decimal.Decimal, arraysize=cursor.arraysize)


def split_success_list(v_max_workers, list_success_table):  # 将创建表成功的list结果分为n个小list，无论指定多少进程，现在最大限制到4进程
    new_list = []  # 用于存储1分为2的表，将原表分成2个list
    if v_max_workers > 4:  # 最大使用4进程分割list
        v_max_workers = 4
    if len(list_success_table) <= 1:
        v_max_workers = 1
    split_size = round(len(list_success_table) / v_max_workers)
    if split_size == 0:  # 防止在如下调用list_of_groups进行切片的时候遇到0
        split_size = 1
    new_list.append(list_of_groups(list_success_table, split_size))
    return new_list


def list_of_groups(init_list, childern_list_len):
    """
    init_list为初始化的列表，childern_list_len初始化列表中的几个数据组成一个小列表
    把一个大list按照切片大小分割，比如5个元素list[a,b,c,d,e],按照切片大小2切割后，就是[[a,b],[c,d],[e]]
    :param init_list:
    :param childern_list_len:
    :return:
    """
    list_of_group = zip(*(iter(init_list),) * childern_list_len)
    end_list = [list(i) for i in list_of_group]
    count = len(init_list) % childern_list_len
    end_list.append(init_list[-count:]) if count != 0 else end_list
    return end_list


# 指定-d选项生效，进行分页查询迁移，并且比对源库和目标库表结构，只迁移源库和目标库共同拥有的列字段，此方式会在迁移前truncate表
def mig_part_tbl_columns(log_path):
    mysql_con = configDB.MySQLPOOL.connection()
    mysql_cur = mysql_con.cursor()  # MySQL连接池
    mysql_cur.arraysize = row_batch_size
    ora_conn = configDB.ora_conn  # 读取config.ini文件中ora_conn变量的值
    source_db = cx_Oracle.connect(ora_conn)  # 源库Oracle的数据库连接
    cur_oracle_result = source_db.cursor()  # 查询Oracle源表的游标结果集
    cur_oracle_result.prefetchrows = row_batch_size
    cur_oracle_result.arraysize = row_batch_size  # Oracle数据库游标对象结果集返回的行数即每次获取多少行
    cur_oracle_result.outputtypehandler = dataconvert
    fetch_many_count = row_batch_size
    err_count = 0
    list_index = 1
    try:
        # 创建迁移任务表，用来统计表插入以及完成的时间
        mysql_cur.execute("""drop table if exists my_mig_task_info""")
        mysql_cur.execute("""create table my_mig_task_info(table_name varchar(100),task_start_time datetime,
                task_end_time datetime ,thread int,run_time decimal(30,6),source_table_rows int,target_table_rows int,
                is_success varchar(100))""")
    except Exception:
        print(traceback.format_exc())
    with open(log_path + "table.txt", "r") as f:  # 读取自定义表
        for table_name in f.readlines():  # 按顺序读取每一个表
            table_name = table_name.strip('\n').upper()  # 去掉列表中每一个元素的换行符
            target_table = source_table = table_name
            target_effectrow = 0
            mysql_insert_count = 0
            get_table_count = 0
            is_success = 0
            concat_col_source = ''  # 记录Oracle以及MySQL共同存在的列名并且用双引号包围
            concat_target_source = ''  # 记录Oracle以及MySQL共同存在的列名并且用"`"包围
            col_map_times = 0  # 源表跟目标表共同拥有的列名
            # 在迁移数据之前先在oracle以及mysql比对下列字段，仅迁移oracle列在MySQL存在的部分
            try:
                cur_oracle_result.execute("""select count(*) from user_tables where table_name='%s' """ % table_name)
                source_tab_exist = cur_oracle_result.fetchone()[0]
                mysql_cur.execute(
                    """select count(*) from information_schema.TABLES where table_schema=database() and table_name='%s' """ % table_name)
                target_tab_exist = mysql_cur.fetchone()[0]
                # 仅针对oracle以及MySQL表都存在的情况下，进行比较列
                if source_tab_exist == 1 and target_tab_exist == 1:
                    try:  # 获取oracle的列字段信息
                        cur_oracle_result.execute(
                            """select column_name from user_tab_columns where table_name='%s' """ % table_name)
                        for source_col_name in cur_oracle_result.fetchall():
                            # 下面接着根据oracle的列名在MySQL比对下是否存在
                            try:
                                mysql_cur.execute("""select count(*) from information_schema.columns where table_schema=database() and 
                                table_name='%s' and column_name='%s' """ % (table_name, source_col_name[0]))
                                target_col_exist = mysql_cur.fetchone()[0]
                                # 如果Oracle的列名在MySQL存在,就把列名拼接起来
                                if target_col_exist == 1:
                                    concat_col_source = concat_col_source + '"' + source_col_name[0] + '"' + ','
                                    concat_target_source = concat_target_source + '`' + source_col_name[0] + '`' + ','
                                    col_map_times += 1
                            except Exception as e:
                                print(e)
                    except Exception as e:
                        print(e)
            except Exception as e:
                print(e)
            # 以下对共同存在的列分别使用双引号以及"`"包围，并且去掉字符串结尾的逗号
            concat_col_source = concat_col_source[:-1]
            concat_target_source = concat_target_source[:-1]
            # 以下做分页查询前准备
            try:
                cur_oracle_result.execute("""select count(*) from %s""" % source_table)
                get_table_count = cur_oracle_result.fetchone()[0]
            except Exception as e:
                print('获取源表总数以及列总数失败，请检查是否存在该表或者表名小写！' + table_name)
                err_count += 1
                sql_insert_error = traceback.format_exc()
                filename = log_path + 'insert_failed_table.log'
                f = open(filename, 'a', encoding='utf-8')
                f.write('-' * 50 + str(err_count) + ' ' + table_name + ' INSERT ERROR' + '-' * 50 + '\n')
                f.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + '\n\n')
                f.write(sql_insert_error + '\n\n')
                f.close()
                logging.error(sql_insert_error)  # 插入失败的sql语句输出到文件ddl_failed_table.log
            val_str = ''  # 用于生成批量插入的列字段变量
            for i in range(1, col_map_times):
                val_str = val_str + '%s' + ','
            val_str = val_str + '%s'  # MySQL批量插入语法是 insert into tb_name values(%s,%s,%s,%s)
            # 迁移前截断表
            try:
                mysql_cur.execute("""truncate table %s""" % table_name)
            except Exception as e:
                print(e)
            insert_sql = 'insert into ' + target_table + '(' + str(
                concat_target_source) + ')' + ' values(' + val_str + ')'  # 拼接insert into 目标表 values  #目标表插入语句
            try:
                mysql_cur.execute("""insert into my_mig_task_info(table_name, task_start_time,thread) values ('%s',
                        current_timestamp(3),%s)""" % (table_name, list_index))  # %s占位符的值需要引号包围
            except Exception:
                print('目标表不存在', traceback.format_exc())
            page_size = split_page_size
            total_page_num = round((get_table_count + page_size - 1) / page_size)  # 自动计算总共有几页
            for page_index in range(total_page_num):  # 例如总共有100行记录，每页10条记录，那么需要循环10次
                cur_start_page = page_index + 1  # page_index是从0开始，所以cur_start_page 从1开始
                startnum, endnum = page_set(cur_start_page, page_size)  # 获取分页的起始页码，还有每页的记录数
                # 下面显式把列名列举出来，而不是*，因为分页会多出一列rownum的序号
                select_sql = '''SELECT {col_name} FROM
                                (
                                SELECT A.*, ROWNUM RN
                                FROM (SELECT * FROM \"{table_name}\") A
                                WHERE ROWNUM <= {endnum}
                                )
                                WHERE RN >= {startnum} '''
                # sql查询语句进行赋值
                select_sql = select_sql.format(col_name=concat_col_source, table_name=source_table, startnum=startnum,
                                               endnum=endnum)
                try:
                    cur_oracle_result.execute(select_sql)  # 执行
                except Exception:
                    print(traceback.format_exc() + '查询Oracle源表数据失败，请检查是否存在该表或者表名小写！\n\n' + table_name)
                    continue  # 这里需要显式指定continue，否则某张表不存在就会跳出此函数
                while True:
                    rows = list(
                        cur_oracle_result.fetchmany(fetch_many_count))
                    try:
                        mysql_cur.executemany(insert_sql, rows)  # 批量插入每次5000行，需要注意的是 rows 必须是 list [] 数据类型
                        mysql_insert_count = mysql_insert_count + mysql_cur.rowcount  # 每次插入的行数
                        mysql_con.commit()  # 如果连接池没有配置自动提交，否则这里需要显式提交
                    except Exception as e:
                        err_count += 1
                        sql_insert_error = '\n' + '/* ' + str(e.args) + ' */' + '\n'
                        filename = log_path + 'insert_failed_table.log'
                        f = open(filename, 'a', encoding='utf-8')
                        f.write('\n-- ' + str(err_count) + ' ' + table_name + ' INSERT ERROR' + '\n')
                        f.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + '\n\n')
                        f.write(insert_sql + '\n\n\n')
                        f.write(str(rows[0]) + '\n\n')
                        f.write(sql_insert_error + '\n\n')
                        f.close()
                    if not rows:
                        break  # 当前表游标获取不到数据之后中断循环，返回到mig_database，可以继续下个表
                    # source_effectrow = cur_oracle_result.rowcount  # 计数源表插入的行数
                    target_effectrow = target_effectrow + mysql_cur.rowcount  # 计数目标表插入的行数
                if get_table_count == target_effectrow:
                    is_success = 'Y'
                else:
                    is_success = 'N'
                print(
                    f'[{table_name}]  source rows：{get_table_count} target rows：{target_effectrow}  THREAD {list_index} {str(datetime.datetime.now())}\n',
                    end='')
            try:
                mysql_cur.execute("""update my_mig_task_info set task_end_time=current_timestamp(3), 
                        source_table_rows=%s,
                        target_table_rows=%s,
                        is_success='%s' where table_name='%s'""" % (
                    get_table_count, target_effectrow, is_success, table_name))  # 占位符需要引号包围
                mysql_con.commit()
            except Exception:
                print(traceback.format_exc())


def isNumber(num):
    """
    判断是否为数字
    :param num:
    :return:
    """
    pattern = re.compile(r'^[-+]?[-0-9]\d*\.\d*|[-+]?\.?[0-9]\d*$')
    result = pattern.match(str(num))
    if result:
        return True
    else:
        return False


def page_set(pageNum, pageSize):
    """
    oracle 查询，分页处理
    :param pageNum:
    :param pageSize:
    :return:
    """
    if not isNumber(pageNum):
        pageNum = 0
    if not isNumber(pageSize):
        pageSize = 0
    pageNum = int(pageNum)
    pageSize = int(pageSize)
    startnum = pageNum * pageSize
    if pageNum > 0:
        startnum = ((pageNum - 1) * pageSize) + 1
    endnum = startnum + pageSize - 1
    return startnum, endnum


def mig_table(task_id, table_list, log_path):  # 这里是子进程,会被主进程multi_process_mig_table调用
    err_count = 0
    # MySQL read config
    mysql_host = configDB.mysql_host
    mysql_port = configDB.mysql_port
    mysql_user = configDB.mysql_user
    mysql_passwd = configDB.mysql_passwd
    mysql_database = configDB.mysql_database
    mysql_dbchar = configDB.mysql_dbchar
    ora_conn = configDB.ora_conn  # 读取config.ini文件中ora_conn变量的值
    source_db_total = cx_Oracle.connect(ora_conn)  # 子进程需要单独设置连接
    cur_oracle_result_split = source_db_total.cursor()  # 查询Oracle源表的游标结果集
    cur_oracle_result_split.outputtypehandler = dataconvert
    cur_oracle_result_split.prefetchrows = row_batch_size
    cur_oracle_result_split.arraysize = row_batch_size  # Oracle数据库游标对象结果集返回的行数即每次获取多少行
    fetch_many_count = row_batch_size
    mysql_con_total = pymysql.connect(host=mysql_host, user=mysql_user, password=mysql_passwd, database=mysql_database,
                                      charset=mysql_dbchar)
    mysql_cursor_total = mysql_con_total.cursor()
    mysql_cursor_total.arraysize = row_batch_size
    for v_table_name in table_list:  # 获取每个进程表名的结果集
        table_name = v_table_name
        target_table = source_table = table_name
        source_effectrow = 0
        target_effectrow = 0
        mysql_insert_count = 0
        source_table_rows = 0
        col_name = ''
        try:
            cur_oracle_result_split.execute("""select count(*) from \"%s\"""" % source_table)
            get_table_count = int(cur_oracle_result_split.fetchone()[0])
            get_column_length = 'select count(*) from user_tab_columns where table_name= ' + "'" + source_table.upper() + "'"  # 拼接获取源表有多少个列的SQL
            cur_oracle_result_split.execute(get_column_length)
            col_len = int(cur_oracle_result_split.fetchone()[0])  # 获取源表有多少个列 oracle连接池
            # 以下是通过一条sql生成列名字段拼接，不调用pandas方法
            try:
                # 如果用listagg某些表有几百列，会造成拼接的字符串过长溢出varchar（4000），现在改用xmlagg生成的clob拼接字段
                # cur_oracle_result_split.execute("""select listagg('"'||column_name||'"',',')  within group(order by COLUMN_ID ) from user_tab_columns where table_name='%s'""" % source_table)
                cur_oracle_result_split.execute(
                    """select trim(',' from (xmlagg(xmlparse(content '"'||column_name||'"'||',') order by COLUMN_ID).getclobval()))  from user_tab_columns where table_name='%s'""" % source_table)
                col_name = cur_oracle_result_split.fetchone()[0]
            except Exception as e:
                print(e, '获取列名失败')
                source_effectrow = -1
                err_count += 1
                sql_insert_error = '\n' + '/* ' + str(e) + ' */' + '\n'
                filename = log_path + 'insert_failed_table.log'
                f = open(filename, 'a', encoding='utf-8')
                f.write('\n-- ' + str(
                    err_count) + ' ' + table_name + ' SELECT SOURCE TABLE OR FETCH COLUMN NAME ERROR' + '\n')
                f.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + '\n\n')
                f.write(sql_insert_error + '\n\n')
                f.close()
        except Exception as e:
            print(traceback.format_exc() + '获取源表总数以及列总数失败，请检查是否存在该表或者表名小写！' + table_name)
            err_count += 1
            filename = log_path + 'insert_failed_table.log'
            f = open(filename, 'a', encoding='utf-8')
            f.write('-' * 50 + str(err_count) + ' ' + table_name + ' INSERT ERROR' + '-' * 50 + '\n')
            f.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + '\n\n')
            f.write(str(e))
            f.close()
            continue  # 这里需要显式指定continue，否则表不存在或者其他问题，会直接跳出for循环
        val_str = ''  # 用于生成批量插入的列字段变量
        for i in range(1, col_len):
            val_str = val_str + '%s' + ','
        val_str = val_str + '%s'  # MySQL批量插入语法是 insert into tb_name values(%s,%s,%s,%s)
        insert_sql = 'insert  into ' + target_table + ' values(' + val_str + ')'  # 拼接insert into 目标表 values  #目标表插入语句
        try:
            mysql_cursor_total.execute(
                """insert into my_mig_task_info(table_name,thread,run_status) values('%s','%s','%s')""" % (
                    table_name, task_id, 'running'))
            mysql_con_total.commit()
        except Exception as e:
            print(e)
        page_size = split_page_size  # 分页的每页记录数
        total_page_num = round((get_table_count + page_size - 1) / page_size)  # 自动计算总共有几页
        for page_index in range(total_page_num):  # 例如总共有100行记录，每页10条记录，那么需要循环10次
            cur_start_page = page_index + 1  # page_index是从0开始，所以cur_start_page 从1开始
            startnum, endnum = page_set(cur_start_page, page_size)  # 获取分页的起始页码，还有每页的记录数
            # 下面显式把列名列举出来，而不是*，因为分页会多出一列rownum的序号
            select_sql = '''SELECT {col_name} FROM
                        (
                        SELECT A.*, ROWNUM RN
                        FROM (SELECT * FROM \"{table_name}\") A
                        WHERE ROWNUM <= {endnum}
                        )
                        WHERE RN >= {startnum} '''
            # sql查询语句进行赋值
            select_sql = select_sql.format(col_name=col_name, table_name=source_table, startnum=startnum, endnum=endnum)
            try:
                cur_oracle_result_split.execute(select_sql)  # 执行
            except Exception:
                print(traceback.format_exc() + '查询Oracle源表数据失败，请检查是否存在该表或者表名小写！\n\n' + table_name)
                continue  # 这里需要显式指定continue，否则某张表不存在就会跳出此函数
            while True:
                rows = list(
                    cur_oracle_result_split.fetchmany(
                        fetch_many_count))
                if not rows:
                    break  # 当前表游标获取不到数据之后中断循环，返回到mig_database，可以继续下个表
                try:
                    mysql_cursor_total.executemany(insert_sql, rows)  # 批量插入获取的结果集，需要注意的是 rows 必须是 list [] 数据类型
                    source_effectrow = source_effectrow + cur_oracle_result_split.rowcount  # 计数源表插入的行数
                    target_effectrow = target_effectrow + mysql_cursor_total.rowcount  # 计数目标表插入的行数
                    mysql_cursor_total.execute(
                        """update my_mig_task_info set source_table_rows='%s',target_table_rows='%s' where table_name='%s'""" % (
                            get_table_count, target_effectrow, table_name))
                    mysql_con_total.commit()
                    mysql_insert_count = mysql_insert_count + mysql_cursor_total.rowcount  # 每次插入的行数
                except Exception as e:
                    target_effectrow = -1
                    mysql_cursor_total.execute(
                        """update my_mig_task_info set source_table_rows='%s',target_table_rows='%s' where table_name='%s'""" % (
                            get_table_count, target_effectrow, table_name))
                    mysql_con_total.commit()
                    err_count += 1
                    print('\n' + '/*  ' + table_name + '  ' + str(e.args) + ' */' + '\n')
                    sql_insert_error = '\n' + '/* ' + str(e.args) + ' */' + '\n'
                    filename = log_path + 'insert_failed_table.log'
                    f = open(filename, 'a', encoding='utf-8')
                    f.write('\n-- ' + str(err_count) + ' ' + table_name + ' INSERT ERROR' + '\n')
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + '\n\n')
                    f.write(insert_sql + '\n\n\n')
                    f.write(str(rows[0]) + '\n\n')
                    f.write(sql_insert_error + '\n\n')
                    f.close()
                try:
                    mysql_cursor_total.execute(
                        """select table_name,thread,source_table_rows,target_table_rows from my_mig_task_info where run_status='running' 
                        and table_name='%s' and thread='%s' """ % (table_name, task_id))
                    for v_out in mysql_cursor_total.fetchall():
                        table_name = v_out[0]
                        thread = v_out[1]
                        source_table_rows = v_out[2]
                        target_table_rows = v_out[3]
                        print(
                            "{0} current migrating progress: {1} thread: {2} source_table_row: {3} target_table_rows: {4}".format(
                                str(datetime.datetime.now()), table_name, thread, source_table_rows, target_table_rows))
                except Exception as e:
                    print(e)
        if source_effectrow == target_effectrow:
            is_success = 'Y'
        else:
            is_success = 'N'
        try:
            mysql_cursor_total.execute("""update my_mig_task_info set run_status='end',
                source_table_rows=%s,
                target_table_rows=%s,
                is_success='%s' where table_name='%s'""" % (
                source_table_rows, target_effectrow, is_success, table_name))  # 占位符需要引号包围
            mysql_con_total.commit()
        except Exception as e:
            print(e)


def multi_process_mig_table(new_list, log_path):  # 这里是主进程,多进程时调用子进程mig_table并行迁移数据
    mysql_cursor = configDB.MySQLPOOL.connection().cursor()  # MySQL连接池
    mysql_cursor.arraysize = row_batch_size
    process_list = []
    print('START MIGRATING ROW DATA! ' + str(datetime.datetime.now()) + ' \n')
    begin_time = datetime.datetime.now()
    for p_id in range(len(new_list[0])):  # new_list被分割的小list个数
        process = multiprocessing.Process(target=mig_table,
                                          args=(
                                              p_id, new_list[0][p_id],
                                              log_path))  # p_id，任务序列，new_list[0][p_id])，小list的表
        process_list.append(process)
    [p.start() for p in process_list]  # 开启了n个进程
    [p.join() for p in process_list]  # 等待两个进程依次结束
    end_time = datetime.datetime.now()
    print('FINISH MIGRATING! ' + str(datetime.datetime.now()) + ' \n')
    print('ELAPSED TIME：' + str((end_time - begin_time).seconds) + '\n')
    #  计算每张表插入时间
    try:
        mysql_cursor.execute(
            """update my_mig_task_info set run_time=(UNIX_TIMESTAMP(task_end_time) - UNIX_TIMESTAMP(task_start_time))""")
        mysql_cursor.execute("""commit""")
    except Exception:
        print('compute my_mig_task_info error')


def main():
    os.environ['NLS_LANG'] = 'SIMPLIFIED CHINESE_CHINA.UTF8'  # 设置字符集为UTF8，防止中文乱码
    multiprocessing.freeze_support()  # windows环境的多进程需要在main函数下面使用此方法，否则程序会被从头开始不断循环
    mig_start_time = datetime.datetime.now()
    all_table_count = 0
    list_success_table = []
    ddl_failed_table_result = []
    all_constraints_count = 0
    all_constraints_success_count = 0
    function_based_index_count = 0
    constraint_failed_count = 0
    all_fk_count = 0
    all_fk_success_count = 0
    foreignkey_failed_count = 0
    all_inc_col_success_count = 0
    all_inc_col_failed_count = 0
    normal_trigger_count = 0
    trigger_success_count = 0
    oracle_autocol_total = 0
    trigger_failed_count = 0
    all_view_count = 0
    all_view_success_count = 0
    all_view_failed_count = 0
    view_failed_result = 0
    run_method = 0
    mode = 0
    is_custom_table = 0
    log_path = ''
    # 同时迁移表数据的并行度,可指定并行度，默认为2
    if args.parallel_degree:
        degree = args.parallel_degree
    else:
        degree = 2
    # 如果指定表迁移，在迁移前输出要迁移的表名称
    if str(args.custom_table).upper() == 'TRUE' or str(args.data_only).upper() == 'TRUE':
        run_method = 1
    # 检测是否是静默模式
    if str(args.quite_mode).upper() == 'TRUE':
        mode = 1
    # 检测是否指定-c参数
    if str(args.custom_table).upper() == 'TRUE':
        is_custom_table = 1
    # 创建日志文件夹
    theTime = datetime.datetime.now()
    theTime = str(theTime)
    date_split = theTime.strip().split(' ')
    date_today = date_split[0].replace('-', '_')
    date_clock = date_split[-1]
    date_clock = date_clock.strip().split('.')[0]
    date_clock = date_clock.replace(':', '_')
    date_show = date_today + '_' + date_clock
    if platform.system().upper() == 'WINDOWS':
        # 设置Oracle客户端的环境变量
        oracle_home = os.path.dirname(os.path.realpath(sys.argv[0])) + '\\oracle_client'
        os.environ['oracle_home'] = oracle_home  # 设置环境变量，如果当前系统存在多个Oracle客户端，需要设置下此次运行的客户端路径
        os.environ['path'] = oracle_home + ';' + os.environ['path']  # 设置环境变量，Oracle客户端可执行文件路径
        os.environ['LANG'] = 'zh_CN.UTF8'  # 需要设置语言环境变量，在部分机器上可能会有乱码
        os.environ['NLS_LANG'] = 'AMERICAN_AMERICA.AL32UTF8'  # 需要设置语言环境变量，在部分机器上可能会有乱码
        # print('oracle_home:',os.environ['oracle_home'],' path:',os.environ['path'])
        # cx_Oracle.init_oracle_client(lib_dir=r"D:\tool\oracle_client")
        log_path = "mig_log" + '\\' + date_show + '\\'
        if not os.path.isdir(log_path):
            os.makedirs(log_path)
    elif platform.system().upper() == 'LINUX':
        log_path = "mig_log" + '/' + date_show + '/'
        if not os.path.isdir(log_path):
            os.makedirs(log_path)
    else:
        print('can not create dir,please run on win or linux!\n')
    # 判断命令行参数-c 或者 -d是否指定
    if str(args.custom_table).upper() == 'TRUE' or str(args.data_only).upper() == 'TRUE':
        path_file = log_path + 'table.txt'  # 用来记录DDL创建成功的表
        if os.path.exists(path_file):
            os.remove(path_file)
        with open('custom_table.txt', 'r', encoding='utf-8') as fr, open(log_path + 'table.txt', 'w',
                                                                         encoding='utf-8') as fd:
            row_count = len(fr.readlines())
        if row_count < 1:
            print('!!!请检查当前目录custom_table.txt是否有表名!!!\n\n\n')
            time.sleep(2)
            sys.exit(0)
        #  在当前目录下编辑custom_table.txt，然后对该文件做去掉空行处理，输出到tmp目录
        with open('custom_table.txt', 'r', encoding='utf-8') as fr, open(log_path + 'table.txt', 'w',
                                                                         encoding='utf-8') as fd:
            for text in fr.readlines():
                if text.split():
                    fd.write(text)
    sys.stdout = Logger(log_path + "\\mig.log", sys.stdout)
    get_info(run_method, mode, log_path)
    # 创建目标表结构
    if str(args.data_only).upper() != 'TRUE':
        all_table_count, list_success_table, ddl_failed_table_result = cte_tab(log_path, is_custom_table)
        new_list = split_success_list(degree, list_success_table)
        # 多进程获取源表数据结果集插入到目标库
        if str(args.metadata_only).upper() != 'TRUE':
            multi_process_mig_table(new_list, log_path)  # 默认是全库迁移，分页方式迁移数据，多进程时调用子进程mig_table_task_total
        # 创建约束包括索引
        all_constraints_count, all_constraints_success_count, function_based_index_count, \
        constraint_failed_count = cte_idx(log_path, is_custom_table)
        # 创建外键
        all_fk_count, all_fk_success_count, foreignkey_failed_count = fk(log_path, is_custom_table)
        # 创建触发器包括自增列
        all_inc_col_success_count, all_inc_col_failed_count, normal_trigger_count, \
        trigger_success_count, trigger_failed_count, oracle_autocol_total = cte_trg(log_path, is_custom_table)
        # 创建comment
        cte_comt(log_path, is_custom_table)
    # 仅迁移表数据
    if str(args.data_only).upper() != 'FALSE' and str(args.metadata_only).upper() != 'TRUE':  # 只有指定了-d选项才会执行此单步迁移
        mig_part_tbl_columns(log_path)  # -d 选项进行分页查询迁移，并且比对源库和目标库表结构，只迁移共同拥有的列字段，此方式会在迁移前truncate表
    # 编译视图以及创建目标视图
    if str(args.data_only).upper() != 'TRUE' and str(args.custom_table).upper() != 'TRUE':
        cp_vw()
        all_view_count, all_view_success_count, all_view_failed_count, view_failed_result = c_vw(log_path,
                                                                                                 is_custom_table)
    func_proc(log_path)
    mig_end_time = datetime.datetime.now()
    run_info(log_path, mig_start_time, mig_end_time, all_table_count, list_success_table, ddl_failed_table_result,
             all_constraints_count, all_constraints_success_count, function_based_index_count,
             constraint_failed_count, all_fk_count, all_fk_success_count, foreignkey_failed_count,
             all_inc_col_success_count, all_inc_col_failed_count, normal_trigger_count, trigger_success_count,
             oracle_autocol_total, trigger_failed_count, all_view_count, all_view_success_count,
             all_view_failed_count, view_failed_result)


if __name__ == '__main__':
    main()
