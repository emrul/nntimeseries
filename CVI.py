import os
os.chdir('C://Users//mbinkowski//cdsol-r-d.cluster//cdsol-r-d.machine_learning_studies//nntimeseries')
from __init__ import *
import utils
import household_data_utils as hdu

log=False

param_dict = dict(
    verbose = [1 + int(log)],
    input_length = [60],
    output_length = [1],
    patience = [5],
    filters = [16],
    act = ['linear'],
    dropout = [(0, 0)],#, (0, 0), (.5, 0)],
    kernelsize = [[3, 1]],
    layers_no = [10],
    poolsize = [None],
    architecture = [{'softmax': True, 'lambda': False, 'nonneg': False}],
    batch_size = [128],
    target_cols=[['Global_active_power', 'Global_reactive_power', 'Voltage',
                  'Global_intensity', 'Sub_metering_1', 'Sub_metering_2',
                  'Sub_metering_3']],
    objective=['regr'],
    norm = [1],
    nonnegative = [False],
    connection_freq = [0, 2],
    aux_weight = [0.0, 0.1],
    shared_final_weights = [False],
    resnet = [False]
)

datasets = ['household.pkl']
save_file = 'results/cvi.pkl' 

def VI(datasource, params):
    globals().update(params)
    G = hdu.HouseholdGenerator(filename=datasource, 
                              input_length=input_length, 
                              output_length=output_length, 
                              verbose=verbose,
                              batch_size=batch_size)
    
    dim = G.asarray().shape[1]
    cols = [i for i, c in enumerate(G.cnames) if c in target_cols]
    regr_func = utils.make_cvi_regression(input_length=input_length, cols=cols)

    inp = Input(shape=(input_length, dim), dtype='float32', name='inp')
    value_input = Input(shape=(input_length, len(cols)), dtype='float32', name='value_input')
    
    offsets = [inp]
    sigs = [inp]
    loop_layers = {}
    
    for j in range(layers_no):
        # significance
        name = 'significance' + str(j+1)
        ks = kernelsize[j % len(kernelsize)] if (type(kernelsize) == list) else kernelsize
        loop_layers[name] = Convolution1D(filters if (j < layers_no - 1) else len(cols), 
                                          filter_length=ks, border_mode='same', 
                                          activation='linear', name=name,
                                          W_constraint=maxnorm(norm))
        sigs.append(loop_layers[name](sigs[-1]))
        
        loop_layers[name + 'BN'] = BatchNormalization(name=name + 'BN')
        sigs.append(loop_layers[name + 'BN'](sigs[-1]))
        
        # residual connections
        if resnet and (connection_freq > 0) and (j > 0) and ((j+1) % connection_freq == 0):
            sigs.append(merge([sigs[-1], sigs[-3 * connection_freq + (j==1)]], mode='sum', 
                               concat_axis=-1, name='significance_residual' + str(j+1)))
                       
        loop_layers[name + 'act'] = LeakyReLU(alpha=.1, name=name + 'act') if (act == 'leakyrelu') else Activation(act, name=name + 'act')
        sigs.append(loop_layers[name + 'act'](sigs[-1]))
        
        # offset
        name = 'offset' + str(j+1)
        loop_layers[name] = Convolution1D(filters if (j < layers_no - 1) else len(cols),
                                          filter_length=1, border_mode='same', 
                                          activation='linear', name=name,
                                          W_constraint=maxnorm(norm))
        offsets.append(loop_layers[name](offsets[-1]))
        
        loop_layers[name + 'BN'] = BatchNormalization(name=name + 'BN')
        offsets.append(loop_layers[name + 'BN'](offsets[-1]))
        
        # residual connections
        if resnet and (connection_freq > 0) and (j > 0) and ((j+1) % connection_freq == 0):
            offsets.append(merge([offsets[-1], offsets[-3 * connection_freq + (j==1)]], mode='sum', 
                                  concat_axis=-1, name='offset_residual' + str(j+1)))
                        
        loop_layers[name + 'act'] = LeakyReLU(alpha=.1, name=name + 'act') if (act == 'leakyrelu') else Activation(act, name=name + 'act')
        offsets.append(loop_layers[name + 'act'](offsets[-1]))
        
        # offset -> significance connection
        if connection_freq > 0:
            if ((j+1) % connection_freq == 0) and (j+1 < layers_no):    
                sigs.append(merge([offsets[-1], sigs[-1]], mode='concat', concat_axis=-1, name='concat' + str(j+1)))
            
    value_output = merge([offsets[-1], value_input], mode='sum', concat_axis=-1, name='value_output')

    value = Permute((2,1))(value_output)

    sig = Permute((2,1))(sigs[-1])
#    sig = TimeDistributed(Dense(input_length, activation='softmax'), name='softmax')(sig) ## SHOULD BE UNNECESSARY, GAVE GOOD RESULTS. SIMILAR PERFORMANCE WITHOUT.
    sig = TimeDistributed(Activation('softmax'), name='softmax')(sig)
    
    main = merge([sig, value], mode='mul', concat_axis=-1, name='significancemerge')
    if shared_final_weights:
        out = TimeDistributed(Dense(output_length, activation='linear', bias=False,
                                    W_constraint=nonneg() if nonnegative else None),
                              name= 'out')(main)
    else: 
        out = LocallyConnected1D(nb_filter=1, filter_length=1,   # dimensions permuted. time dimension treated as separate channels, no connections between different features
                                 border_mode='valid')(main)
        
    main_output = Permute((2,1), name='main_output')(out)
    
    nn = Model(input=[inp, value_input], output=[main_output, value_output])
    
    nn.compile(optimizer=keras.optimizers.Adam(lr=.001),
               loss={'main_output': 'mse', 'value_output' : 'mse'},
               loss_weights={'main_output': 1., 'value_output': aux_weight}) 

    train_gen = G.gen('train', func=regr_func)
    valid_gen = G.gen('valid', func=regr_func)
    reducer = LrReducer(patience=patience, reduce_rate=.1, reduce_nb=3, 
                        verbose=1, monitor='val_main_output_loss', restore_best=True)
    
    print('Total model parameters: %d' % int(np.sum([np.sum([np.prod(K.eval(w).shape) for w in l.trainable_weights]) for l in nn.layers])))
    
    length = input_length + output_length
    hist = nn.fit_generator(
        train_gen,
        samples_per_epoch = G.n_train - length-1,
        nb_epoch=7,
        callbacks=[reducer],
    #            callbacks=[callback, keras.callbacks.EarlyStopping(monitor='val_loss', patience=patience)],
        validation_data=valid_gen,
        nb_val_samples=G.n_all - G.n_train - length-1,
        verbose=verbose
    )    
    return hist, nn, reducer
    
runner = utils.ModelRunner(param_dict, datasets, VI, save_file)
runner.run(log=log)