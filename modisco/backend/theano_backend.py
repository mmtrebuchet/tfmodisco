from __future__ import division, print_function
import theano
from theano import tensor as T
from theano.tensor import signal
from theano.tensor.signal import pool
import numpy as np
import sys


def run_function_in_batches(func,
                            input_data_list,
                            learning_phase=None,
                            batch_size=10,
                            progress_update=1000,
                            multimodal_output=False):
    #func has a return value such that the first index is the
    #batch. This function will run func in batches on the inputData
    #and will extend the result into one big list.
    #if multimodal_output=True, func has a return value such that first
    #index is the mode and second index is the batch
    assert isinstance(input_data_list, list), "input_data_list must be a list"
    #input_datas is an array of the different input_data modes.
    to_return = [];
    i = 0;
    while i < len(input_data_list[0]):
        if (progress_update is not None):
            if (i%progress_update == 0):
                print("Done",i)
                sys.stdout.flush()
        func_output = func(*([x[i:i+batch_size] for x in input_data_list]
                                +([] if learning_phase is
                                   None else [learning_phase])
                        ))
        if (multimodal_output):
            assert isinstance(func_output, list),\
             "multimodal_output=True yet function return value is not a list"
            if (len(to_return)==0):
                to_return = [[] for x in func_output]
            for to_extend, batch_results in zip(to_return, func_output):
                to_extend.extend(batch_results)
        else:
            to_return.extend(func_output)
        i += batch_size;
    return to_return


def tensor_with_dims(num_dims, name):
    return T.TensorType(dtype=theano.config.floatX,
                        broadcastable=[False]*num_dims)(name)


def get_window_sum_function(window_size,same_size_return):
    """
        Returns a function for smoothening inputs with a window
         of size window_size.

        Returned function has arguments of inp,
         batch_size and progress_update
    """
    orig_inp_tensor = tensor_with_dims(2, "inp_tensor") 
    inp_tensor = orig_inp_tensor[:,None,None,:]

    averaged_inp = theano.tensor.signal.pool.pool_2d(
                        input=inp_tensor,
                        ws=(1,window_size),
                        ignore_border=True,
                        stride=(1,1),
                        pad=(0,0 if (not same_size_return)
                               else int(window_size/2)),
                        mode='average_exc_pad') 

    #if window_size is even, then we have an extra value in the output,
    #so kick off the value from the front
    if (window_size%2==0 and same_size_return):
        averaged_inp = averaged_inp[:,:,:,1:]

    averaged_inp = averaged_inp[:,0,0,:]
    smoothen_func = theano.function([orig_inp_tensor],
                                    averaged_inp*window_size,
                                    allow_input_downcast=True)

    def smoothen(inp, batch_size, progress_update=None):
       return run_function_in_batches(
                func=smoothen_func,
                input_data_list=[inp],
                batch_size=batch_size,
                progress_update=progress_update)

    return smoothen


def get_argmax_function(): 
    inp_tensor = tensor_with_dims(2, "inp_tensor") 
    argmaxes = T.argmax(inp_tensor, axis=1) 
    argmax_func = theano.function([inp_tensor], argmaxes,
                                  allow_input_downcast=True)
    def argmax_func_wrapper(inp, batch_size, progress_update=None):
        return run_function_in_batches(
                func=argmax_func,
                input_data_list=[inp],
                batch_size=batch_size,
                progress_update=progress_update)
    return argmax_func_wrapper


def get_gapped_kmer_embedding_func(filters, biases, require_onehot_match):

    #filters should be: out_channels, rows, ACGT
    filters = filters.astype("float32")
    biases = biases.astype("float32")
    if (require_onehot_match):
        onehot_var = theano.tensor.TensorType(dtype=theano.config.floatX,
                                          broadcastable=[False]*3)("onehot")
    toembed_var = theano.tensor.TensorType(dtype=theano.config.floatX,
                                           broadcastable=[False]*3)("toembed")
    theano_filters = theano.tensor.as_tensor_variable(
                      x=filters, name="filters")
    theano_biases = theano.tensor.as_tensor_variable(x=biases, name="biases")
    if (require_onehot_match):
        onehot_out = 1.0*((theano.tensor.nnet.conv2d(
                        input=onehot_var[:,None,:,:],
                        filters=theano_filters[:,None,::-1,::-1],
                        border_mode='valid')[:,:,:,0] + biases[None,:,None])
                        > 0.0)
    embedding_out = theano.tensor.sum((theano.tensor.nnet.conv2d(
                        input=toembed_var[:,None,:,:],
                        filters=theano_filters[:,None,::-1,::-1],
                        border_mode='valid')[:,:,:,0])*
                        (onehot_out if require_onehot_match else 1.0), axis=2)
    if (require_onehot_match):
        func = theano.function([onehot_var, toembed_var], embedding_out,
                                allow_input_downcast=True)
        def batchwise_func(onehot, to_embed, batch_size, progress_update):
            return np.array(run_function_in_batches(
                                func=func,
                                input_data_list=[onehot, to_embed],
                                batch_size=batch_size,
                                progress_update=progress_update))
    else:
        func = theano.function([toembed_var], embedding_out,
                                allow_input_downcast=True)
        def batchwise_func(to_embed, batch_size, progress_update):
            return np.array(run_function_in_batches(
                                func=func,
                                input_data_list=[to_embed],
                                batch_size=batch_size,
                                progress_update=progress_update))
    return batchwise_func


def matrix_dot_product(mat1, mat2, batch_size):
    mat1_tensor = theano.tensor.TensorType(dtype=theano.config.floatX,
                                         broadcastable=[False]*2)("mat1")
    result = theano.tensor.dot(mat1_tensor, mat2.astype("float32"))
    dot_product_func = theano.function([mat1_tensor], result,
                                       allow_input_downcast=True)
    return np.array(run_function_in_batches(
                        func=dot_product_func,
                        input_data_list=[mat1],
                        batch_size=batch_size,
                        progress_update=None))


def max_cross_corrs(filters, things_to_scan, min_overlap,
                       batch_size=50,
                       func_params_size=1000000,
                       progress_update=1000):
    """
        func_params_size: when compiling functions
    """
    #reverse the patterns as the func is a conv not a cross corr
    assert len(filters.shape)==3,"Did you pass in filters of unequal len?"
    assert filters.shape[-1]==things_to_scan.shape[-1]
    filters = filters.astype("float32")[:,::-1,::-1]
    to_return = np.zeros((filters.shape[0], len(things_to_scan)))
    #compile the number of filters that result in a function with
    #params equal to func_params_size 
    params_per_filter = np.prod(filters[0].shape)
    filter_batch_size = int(func_params_size/params_per_filter)
    filter_length = filters.shape[1]
    filter_idx = 0 
    while filter_idx < filters.shape[0]:
        if (progress_update is not None):
            print("On filters",filter_idx,"to",
                  min((filter_idx+filter_batch_size),len(filters)))
            sys.stdout.flush()

        filter_batch = filters[filter_idx:
                              min((filter_idx+filter_batch_size),len(filters))]

        padding_amount = int((filter_length)*(1-min_overlap))
        padded_input = [np.pad(array=x,
                              pad_width=((padding_amount, padding_amount),
                                         (0,0)),
                              mode="constant") for x in things_to_scan]

        input_var = theano.tensor.TensorType(dtype=theano.config.floatX,
                                             broadcastable=[False]*3)("input")
        theano_filters = theano.tensor.as_tensor_variable(
                   x=filter_batch, name="filters")
        conv_out = theano.tensor.nnet.conv2d(
                    input=input_var[:,None,:,:],
                    filters=theano_filters[:,None,::-1,::-1],
                    border_mode='valid')[:,:,:,0]

        max_out = T.max(conv_out, axis=-1)

        max_cross_corr_func = theano.function([input_var], max_out,
                               allow_input_downcast=True)

        max_cross_corrs = np.array(run_function_in_batches(
                            func=max_cross_corr_func,
                            input_data_list=[padded_input],
                            batch_size=batch_size,
                            progress_update=progress_update))
        assert len(max_cross_corrs.shape)==2, max_cross_corrs.shape
        to_return[filter_idx:
                  min((filter_idx+filter_batch_size),len(filters)),:] =\
                  np.transpose(max_cross_corrs)
        filter_idx += filter_batch_size
        
    return to_return


def sign(var):
    return (1.0*(var > 0.0) + -1.0*(var < 0.0))


def get_jaccard_sim_func(filters):
    """
        func_params_size: when compiling functions
    """
    #reverse the patterns as the func is a conv not a cross corr
    assert len(filters.shape)==3,"Did you pass in filters of unequal len?"


    input_var = theano.tensor.TensorType(
                    dtype=theano.config.floatX,
                    broadcastable=[False,False,False])("input")
    intersection = T.sum(T.sum(T.minimum(T.abs_(input_var[None,:,:,:]),
                                         T.abs_(filters[:,None,:,:]))*
                               (sign(input_var[None,:,:,:])*
                                sign(filters[:,None,:,:])),
                         axis=3),axis=2) 
    union = T.sum(T.sum(T.maximum(T.abs_(input_var[None,:,:,:]),
                                  T.abs_(filters[:,None,:,:])),
                  axis=3),axis=2)
    result = intersection/union   

    jaccard_sim_func = theano.function([input_var], result,
                                          allow_input_downcast=True)
        
    return jaccard_sim_func
