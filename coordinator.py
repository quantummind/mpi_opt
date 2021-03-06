import skopt 
import random
import pickle 

from tag_lookup import tag_lookup

class Coordinator(object):
    """
    This class coordinates the Bayesian optimization procedure.
    
    Attributes:
    comm: MPI communicator object containing all processes.
    num_blocks: int, number of blocks of workers
    opt_params: list of parameter ranges, suitable to pass
        to skopt.Optimizer's constructor
    optimizer: skopt.Optimizer instance
    model_fn: python function.  should take a list of parameters
        and return a JSON string representing the keras model to train
    param_list: list of parameter sets tried
    fom_list: list of figure of merit (FOM) values obtained
    block_dict: dict keeping track of which block is running which model
    req_dict: dict holding MPI receive requests for each running block
    best_params: best parameter set found by Bayesian optimization
    """

    def __init__(self, comm, num_blocks,
                 opt_params):
        print("Coordinator initializing")
        self.comm = comm
        self.num_blocks = num_blocks

        self.opt_params = opt_params
        self.optimizer = skopt.Optimizer(dimensions=self.opt_params, random_state= 13579)
        self.param_list = []
        self.fom_list = []
        self.block_dict = {}
        self.req_dict = {}
        self.best_params = None
        self.best_fom = None

        self.next_params = []
        self.to_tell = []
        self.ends_cycle = False
        self.target_fom = None
        
    def ask(self, n_iter):
        if not self.next_params:
            ## don't ask every single time
            self.next_params = self.optimizer.ask( n_iter )
        return self.next_params.pop(-1)

    def save(self, fn = 'coordinator.pkl'):
        d= open(fn,'wb')
        pickle.dump( self.optimizer, d )
        d.close()

    def load(self, fn= 'coordinator.pkl'):
        d = open(fn, 'rb')
        print ("loading the coordinator optimizer from",fn)
        self.optimizer = pickle.load( d )
        d.close()

    def fit(self):
        X = [o[0] for o in self.to_tell]
        Y = [o[1] for o in self.to_tell]

        if X and Y:
            print ("Fitting from {} values".format( len (X)))
            opt_result = self.optimizer.tell( X, Y )
            self.best_params = opt_result.x
            self.best_fom = opt_result.fun
            print("New best param estimate, with telling {} points : {}, with value {}".format(len(X),self.best_params, self.best_fom))
            self.next_params = []
            self.to_tell = []
            ## checkpoint your self
            self.save()
            if self.target_fom and opt_result.fun < self.target_fom:
                print ("the optimization has reached the desired value at optimum",self.target_fom)
                self.ends_cycle = True

    def tell(self, params, result):
        self.to_tell.append( (params, result) )
        tell_right_away = False
        if tell_right_away:
            self.fit()
        
    def run(self, num_iterations=1):
        for step in range(num_iterations):
            print("Coordinator iteration {}".format(step))
            next_block = self.wait_for_idle_block()
            if self.ends_cycle:
                print("Coordinator is skiping the iteration cycle")
                break
            next_params = self.ask( num_iterations )
            print("Next block: {}, next params {}".format(next_block, next_params))
            self.run_block(next_block, next_params)
            
        for proc in range(1, self.comm.Get_size()):
            print("Signaling process {} to exit".format(proc))
            self.comm.send(None, dest=proc, tag=tag_lookup('params'))
        self.comm.Barrier()
        print("Finished all iterations!")
        print("Best parameters found: {} with value {}".format(self.best_params, self.best_fom))
        
    def wait_for_idle_block(self):
        """
        In a loop, checks each block of processes to see if it's
        idle.  This function blocks until there is an available process.
        """
        blocklist = list(range(1, self.num_blocks+1))
        
        while True:
            self.fit()
            random.shuffle( blocklist )
            for cur_block in blocklist:
                idle = self.check_block(cur_block)
                if idle:
                    print ("From coordinator, block {} is found idling, and can be used next".format( cur_block))
                    return cur_block

    def check_block(self, block_num):
        """
        If the indicated block has completed a training run, store the results.
        Returns True if the block is ready to train a new model, False otherwise.
        """
        if block_num in self.block_dict:
            done, result = self.req_dict[block_num].test()
            if done:
                params = self.block_dict.pop(block_num)
                self.param_list.append(params)
                self.fom_list.append(result)
                print("Telling {} at {}".format(result, params))
                self.tell( params, result )
                del self.req_dict[block_num]
                return True
            return False
        else:
            return True

    def run_block(self, block_num, params):
        self.block_dict[block_num] = params
        # In the current setup, we need to signal each GPU in the 
        # block to start training
        block_size = int((self.comm.Get_size()-1)/self.num_blocks)
        start = (block_num-1) * block_size + 1 
        end = block_num * block_size 
        print("Launching block {}. Sending params to nodes from {} to {}".format(block_num, start,end))
        for proc in range(start, end+1):
            self.comm.send(params, dest=proc, tag=tag_lookup('params')) 
        self.req_dict[block_num] = self.comm.irecv(source=start, tag=tag_lookup('result'))
