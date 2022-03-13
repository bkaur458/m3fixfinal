from ast import arg
from traceback import print_tb
from numpy import insert

from torch import stack
from lstore.query import Query
from lstore.table import Table, Record
from lstore.index import Index
import os
from os.path import *

class Transaction:

    """
    # Creates a transaction object.
    """
    def __init__(self): 
        self.queries = []
        #assigns a unique id based on time in milliseconds
        self.transaction_id = 0
        #for now, mainly used to access key_dict and page_dictionary of a table
        self.table = None
        self.stack = []
        #Need a query object to use query functions
        self.query_obj = None

    '''
    Stack logic for now:
    If a query runs successfully:
        add to log and then append to the stack
    If a query fails:
        call abort
    If a commit happens:
        write to disk
        pop all queries from the stack
    If an abort happens:
        Go thru each query in the stack, and undo/ redo as needed
    '''

    """
    # Adds the given query to this transaction
    # Example:
    # q = Query(grades_table)
    # t = Transaction()
    # t.add_query(q.update, grades_table, 0, *[None, 1, None, 2, None])
    """
    def add_query(self, query, table, *args):
        if self.table == None:
            self.table = table
        if self.query_obj == None:
            self.query_obj = Query(self.table)
        if self.transaction_id == 0:
            self.table.transaction_id_ctr += 1
            self.transaction_id = self.table.transaction_id_ctr
        self.queries.append((query, args))
        # use grades_table for aborting

    '''
    Entry in a Log looks like this for now:
    4       bytes - serial #
    4       bytes - xcat id
    4       bytes - action (1 - insert, 2- update, 3- delete)
    4       bytes - RID
    (N+2)*8 bytes - old values for N+2 columns 
    (N+2)*8 bytes - new values for N+2 columns

    Also added a log_serial_no attribute in table class to give serial numbers to each entry
    '''

    '''
    Entry in a stack is a list for each query and looks like this for now:
    Index     Value
    0       - Xcat id
    1       - Action - (1 - insert, 2- update, 3- delete)
    2       - RID 
    3       - List of old values
    4       - List of new values
    '''

    '''
    Formats for args for different query functions:
    t.add_query(q.insert, grades_table, [1,1,1,1,1])
    t.add_query(q.select, grades_table, key, 0, [1, 1, 1, 1, 1])
    t.add_query(q.update, grades_table, key, [None, None, None, None, None])
    t.add_query(q.delete, grades_table, key)
    Any other args after grades_table are stored in a single "args" list
    '''

    # If you choose to implement this differently this method must still return True if transaction commits or False on abort
    def run(self, id):

        fin = open("log.txt", "a+b")
        for query, args in self.queries:

            #print(str(id) + " worker with xcat id: "+ str(self.transaction_id) + " inserting " + str(args))

            # Select could fail if its trying to read a record that another xcat has an exclusive lock for
            if("select" in str(query)):
                all_rids = self.table.index.locate(args[1] + 3, args[0]) 
                for i in range(len(all_rids)):
                    # if False (aka transaction aborted), then return
                    if not self.set_shared_rid(all_rids[i]):
                        print('select aborted')
                        self.abort()

                result = query(*args)
                # If the query has failed the transaction should abort
                if result == False: 
                    return self.abort()
            elif("sum" in str(query)):
                start_range = args[0]
                end_range = args[1]
                sorted_keys = sorted(self.table.key_dict.items())

                for key, rid in sorted_keys:
                    # break if start_range greater than key
                    if start_range > key: continue
                    # break if key greater than end_range
                    if end_range < key: break

                    # if False (aka transaction aborted), then return
                    if not self.set_shared_rid(rid):
                        return

                result = query(*args)
                # If the query has failed the transaction should abort
                if result == False:
                    return self.abort()

            # For insert, update and delete
            else:
                query_entry_for_stack = []

                #Declare old values
                old_values = []

                # Declare new values
                new_values = []

                #1. Adding log_serial_no to log:
                #   Increment log_serial_no first, then insert to log (log_serial_no is initialized as 0 in the table class
                #   so the first entry in log will have a log_serial_no of 1)
                self.table.log_serial_no += 1                        
                fin.write(bytearray((self.table.log_serial_no).to_bytes(4, 'big', signed=True)))

                #2. Adding transaction id to log
                fin.write(bytearray((self.transaction_id).to_bytes(4, 'big', signed=True)))

                #---------ACTION 1 ---------#
                if("insert" in str(query)):
                    # if False (aka transaction aborted), then return
                    # print('rid: ', self.table.rid)
                    if not self.set_exclusive_rid(self.table.rid):
                        self.abort()
                    # print('correct insert')


                    #3. Adding action type
                    fin.write(bytearray((1).to_bytes(4, 'big', signed=True)))
                    query_entry_for_stack.append(1)
                    #4. Adding RID
                    fin.write(bytearray((self.table.rid).to_bytes(4, 'big', signed=True)))
                    query_entry_for_stack.append(args[self.table.key])

                    #5. Adding old values
                    old_values.append(-1)     #just appending -1 to stack as don't have old values for insert
                    for col_no in range(1, self.table.num_columns+3):
                        fin.write(bytearray((0).to_bytes(8, 'big', signed=True)))

                    #6. Adding new values

                    # First write indirection and schema values
                    #For indirection
                    fin.write(bytearray((-1).to_bytes(8, 'big', signed=True)))
                    #For schema encoding
                    fin.write(bytearray((0).to_bytes(8, 'big', signed=True)))

                    #Now all data values
                    for col_no in range(0, len(args)):
                        new_values.append(args[col_no])
                        fin.write(bytearray((args[col_no]).to_bytes(8, 'big', signed=True)))

                #---------ACTION 2 ---------#
                elif("update" in str(query)):
                    rid = self.table.key_dict[args[0]]
                    # if False (aka transaction aborted), then return
                    if not self.set_exclusive_rid(rid):
                        self.abort()

                    #3. Adding action type
                    fin.write(bytearray((2).to_bytes(4, 'big', signed=True)))
                    query_entry_for_stack.append(2)

                    #4. Adding RID
                    primary_key = args[0]
                    fin.write(bytearray((self.table.key_dict[primary_key]).to_bytes(4, 'big', signed=True)))
                    query_entry_for_stack.append(primary_key)

                    #5. Adding old values
                    rid = self.table.key_dict[primary_key]
                    page_range_id, main_page_id, slot_no = self.table.page_directory[rid]
                    #read the old values before query
                    for physical_page_id in range(1, self.table.num_columns+3):
                        page_id = "b" + str(page_range_id) + "-" + str(main_page_id) + "-" + str(physical_page_id) + "-"
                        curr_page = self.table.bufferpool.access(page_id, None)
                        value = curr_page.read(slot_no)
                        old_values.append(value)
                        fin.write(bytearray((value).to_bytes(8, 'big', signed=True)))
                    query_entry_for_stack.append(old_values)

                    #6. Adding new values
                    for col_no in range(1, self.table.num_columns+3):   
                        fin.write(bytearray((0).to_bytes(8, 'big', signed=True)))
        
                #---------ACTION 3 ---------#
                elif("delete" in str(query)):
                    rid = self.table.key_dict[args[0]]
                    # if False (aka transaction aborted), then return
                    if not self.set_exclusive_rid(rid):
                        self.abort()

                    #3. Adding action type
                    fin.write(bytearray((3).to_bytes(4, 'big', signed=True)))
                    query_entry_for_stack.append(3)

                    #4. Adding RID
                    primary_key = args[0]
                    query_entry_for_stack.append(self.table.key_dict[primary_key])
                    fin.write(bytearray((self.table.key_dict[primary_key]).to_bytes(4, 'big', signed=True)))

                    #5. Adding old value
                    rid = self.table.key_dict[args[0]]
                    page_range_id, main_page_id, slot_no = self.table.page_directory[rid]
                    #read the old values before query
                    for physical_page_id in range(1, self.table.num_columns+3):
                        page_id = "b" + str(page_range_id) + "-" + str(main_page_id) + "-" + str(physical_page_id) + "-"
                        curr_page = self.table.bufferpool.access(page_id, None)
                        value = curr_page.read(slot_no)
                        if physical_page_id == 1:
                            query_entry_for_stack.append(value)
                        fin.write(bytearray((value).to_bytes(8, 'big', signed=True)))

                    #6. Adding new values
                    
                    # First write indirection and schema values
                    #For indirection
                    fin.write(bytearray((-1).to_bytes(8, 'big', signed=True)))
                    #For schema encoding
                    fin.write(bytearray((0).to_bytes(8, 'big', signed=True)))

                    #Now all data values
                    for col_no in range(0, len(args)):
                        fin.write(bytearray((0).to_bytes(8, 'big', signed=True)))

                result = query(*args)
                # print(result)

                # If the query has failed the transaction should abort
                if result == False:
                    # if("insert" in str(query)):
                    #     print("Inserted failed on: " + str(args[0]))
                    return self.abort()

                if("insert" in str(query) and args[0]==92106429 ):
                    print("Inserted key: " + str(args[0]))

                #Only if query successful, append to the stack
                self.stack.append(query_entry_for_stack)
                if args[0] == 92106430 and 'delete' in str(query):
                        return self.abort()
        return self.commit()

    def abort(self):
        #We start from the top of the stack. i.e. most recently inserted values for now
        while len(self.stack)>0:
            each_query_entry = self.stack.pop()
            action = each_query_entry[0]
            #1. Undo an insert
                #According to the instructions, any inserted records here need not be removed from the database
                #Can just be marked as deleted
            if action == 1:
                #First get the primary key value which will be the third value in the list of new values
                #after indirection and schema encoding
                self.query_obj.delete(each_query_entry[1])

            #2. Undo an update
            elif action == 2:
                
                #Only changing the schema and indirection value back to the old values for now
                old_values = each_query_entry[2]
                rid = self.table.key_dict[each_query_entry[1]]
                page_range_id, main_page_id, slot_no = self.table.page_directory[rid]

                #For the indirection page
                for physical_page_id in range(1, self.table.num_columns+3):
                    page_id = "b" + str(page_range_id) + "-" + str(main_page_id) + "-" + str(physical_page_id) + "-"
                    curr_page = self.table.bufferpool.access(page_id, None)
                    curr_page.overwrite(slot_no, old_values[physical_page_id - 1])

            #3. Undo a delete
            elif action == 3:
                #get all old values and create a new record with those values
                #Just need to change indirection value back to the old one )
                #First get the rid from the primary key, cannot use key_dict as delete removes the primary key from it
                rid = each_query_entry[1]
                page_range_id, main_page_id, slot_no = self.table.page_directory[rid]

                #TODO: Do we need pincount adjustment here?
                #For the indirection page
                indirection_page_id = "b" + str(page_range_id) + "-" + str(main_page_id) + "-" + str(1) + "-"
                indirection_page = self.table.bufferpool.access(indirection_page_id, None)
                indirection_page.overwrite(slot_no, each_query_entry[0])

                indirection_page_id = "b" + str(page_range_id) + "-" + str(main_page_id) + "-" + str(self.table.key + 3) + "-"
                indirection_page = self.table.bufferpool.access(indirection_page_id, None)

                #Now bring back primary key entry into the key_dict
                self.table.key_dict[indirection_page.read(slot_no)] = rid

        return False

    # TODO: make sure commit release all locks that were acquired by any queries
    def commit(self):

        #clear the stack
        self.stack.clear()

        #release all shared locks
        for each_rid in list(self.table.shared_lock_manager.keys()):
            if self.transaction_id in self.table.shared_lock_manager[each_rid]:
                self.table.shared_lock_manager[each_rid].remove(self.transaction_id)
                if not self.table.shared_lock_manager[each_rid]:
                    del self.table.shared_lock_manager[each_rid]

        #release all exclusive locks 
        for rid in list(self.table.exclusive_lock_manager.keys()):
            if self.transaction_id == self.table.exclusive_lock_manager[rid]:
                del self.table.exclusive_lock_manager[rid]

        return True
        # TODO: commit to database
        for query, args in self.queries:
            # update bufferpool pages
            primary_key = args[0] 
            rid = self.table.key_dict(primary_key)
            page_range_id, main_page_id, slot_no = self.table.page_directory(rid)

            indirection_page_id = "b" + str(page_range_id) + "-" + str(main_page_id) + "-" + str(1) + "-"
            indirection_page = self.table.bufferpool.access(indirection_page_id, None)

            schema_page_id = "b" + str(page_range_id) + "-" + str(main_page_id) + "-" + str(2) + "-"
            schema_page = self.table.bufferpool.access(schema_page_id, None)

            if "insert" in str(query):
                pass
            elif "update" in str(query):
                pass
            elif "delete" in str(query):
                pass

            # mark them dirty
            self.table.bufferpool.mark_page_dirty(indirection_page_id)
            self.table.bufferpool.mark_page_dirty(schema_page_id)

            # write to disk
            path = self.table.bufferpool.path
            pool = self.table.bufferpool.pool
            # write indirection page to disk
            name = os.path.join(path, indirection_page_id + '.txt')
            with open(name, "wb") as f:
                f.write(pool[indirection_page_id].data)
            # write schema page to disk
            name = os.path.join(path, schema_page_id + '.txt')
            with open(name, "wb") as f:
                f.write(pool[schema_page_id].data)

            # mark them non-dirty
            # TODO: function to reverse the mark_page_dirty function, maybe combine it into one?
            
        # pop all queries from the stack <- is there still a need for this if we go through the queries in order
        return True

    def set_exclusive_rid(self, rid):
        # if rid present in lock manager, abort
        if rid in self.table.exclusive_lock_manager.keys():
            return False
        elif rid in self.table.shared_lock_manager.keys():
            if self.transaction_id in self.table.shared_lock_manager[rid] and len(self.table.shared_lock_manager[rid])==1:
                self.table.exclusive_lock_manager[rid] = self.transaction_id
                return True
            else:
                return False
        # else place exclusive lock on rid in lock manager
        else:
            # print('chillin')
            self.table.exclusive_lock_manager[rid] = self.transaction_id
        return True

    def set_shared_rid(self, rid):
        # if rid is exclusively locked, abort
        if rid in self.table.exclusive_lock_manager.keys(): 
            return False
        
        #If rid present in shared lock keys
        if rid in self.table.shared_lock_manager.keys():
            #if this transaction is not present in the list of the xcats that have this shared lock, add it
            if self.transaction_id not in self.table.shared_lock_manager[rid]:
                self.table.shared_lock_manager[rid].append(self.transaction_id)
        #If rid not present in shared lock keys
        else:
            self.table.shared_lock_manager[rid] = []
            self.table.shared_lock_manager[rid].append(self.transaction_id)
        return True 